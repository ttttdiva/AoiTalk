"""フィードバック送信・一覧・解決ルート (server.py から移設)"""

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...features import Features
from ...services.feedback_store import FeedbackEntry
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


class FeedbackSubmitResponse(BaseModel):
    success: bool
    feedback_id: str
    message: str


class FeedbackListResponse(BaseModel):
    feedback: list[FeedbackEntry]
    count: int


class FeedbackResolveResponse(BaseModel):
    success: bool
    message: str


def register_feedback_routes(app: FastAPI, server: "WebChatServer") -> None:
    """フィードバック関連ルートを登録する (JSONL→DB 移行タスクの登録を含む)"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    server._feedback_migration_status = "pending"

    async def require_admin(request: Request) -> dict:
        """Require an authenticated administrator for review operations."""
        if not server.auth_enabled:
            return {"id": "default_user", "username": "default_user", "role": "admin"}
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Authentication required")
        if str(user_info.get("role", "")).lower() != "admin":
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        return user_info

    # ── Feedback API Endpoints ──────────────────────────────────────────
    # Import feedback module (async version for DB support)
    try:
        from ..feedback import (
            FeedbackRequest,
            save_feedback_async,
            load_feedback_async,
            mark_feedback_resolved_async,
            migrate_jsonl_to_database,
        )

        FEEDBACK_AVAILABLE = True

        # Migrate existing JSONL data to database on startup (async)
        # Delay to allow PostgreSQL to fully initialize
        async def _migrate_feedback():
            # Wait for database to be ready
            await asyncio.sleep(5)
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    migrated = await migrate_jsonl_to_database()
                    if migrated > 0:
                        logger.info(
                            f"Migrated {migrated} feedback entries from JSONL to database"
                        )
                    server._feedback_migration_status = "ready"
                    return
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Feedback migration attempt {attempt + 1} failed: {e}, retrying..."
                        )
                        await asyncio.sleep(2)
                    else:
                        logger.warning(
                            f"Failed to migrate feedback after {max_retries} attempts: {e}"
                        )
                        server._feedback_migration_status = "failed"

        server._startup_background_tasks.append(_migrate_feedback)
    except ImportError as e:
        logger.warning(f"Feedback module import failed: {e}")
        FEEDBACK_AVAILABLE = False
        FeedbackRequest = None
        save_feedback_async = None
        load_feedback_async = None
        mark_feedback_resolved_async = None
        server._feedback_migration_status = "failed"

    @app.post("/api/feedback", response_model=FeedbackSubmitResponse)
    async def submit_feedback(request: Request, _: None = Depends(require_auth)):
        """Submit feedback on an agent response"""
        if not FEEDBACK_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Feedback system is not available"
            )

        try:
            body = await request.json()
            feedback_req = FeedbackRequest(**body)
            entry = await save_feedback_async(feedback_req)
            logger.info(f"Feedback submitted: {entry.id} - {entry.category}")
            return JSONResponse(
                {
                    "success": True,
                    "feedback_id": entry.id,
                    "message": "フィードバックを送信しました",
                }
            )
        except Exception as e:
            logger.error(f"Failed to save feedback: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to save feedback: {e}"
            )

    @app.get("/api/feedback", response_model=FeedbackListResponse)
    async def get_feedback_list(
        request: Request,
        include_resolved: bool = False,
        limit: int = Query(100, ge=1, le=500),
        _: None = Depends(require_auth),
    ):
        """Get list of feedback entries (for admin review)"""
        admin_info = await require_admin(request)
        if not FEEDBACK_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Feedback system is not available"
            )

        try:
            entries = await load_feedback_async(
                include_resolved=include_resolved, limit=limit
            )
            return JSONResponse(
                {
                    "feedback": [entry.model_dump() for entry in entries],
                    "count": len(entries),
                }
            )
        except Exception as e:
            logger.error(f"Failed to load feedback: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to load feedback: {e}"
            )

    @app.post(
        "/api/feedback/{feedback_id}/resolve",
        response_model=FeedbackResolveResponse,
    )
    async def resolve_feedback(
        feedback_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Mark a feedback entry as resolved"""
        admin_info = await require_admin(request)
        if not FEEDBACK_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Feedback system is not available"
            )

        try:
            success = await mark_feedback_resolved_async(
                feedback_id, resolved_by=admin_info.get("username")
            )
            if success:
                return JSONResponse(
                    {
                        "success": True,
                        "message": "フィードバックを解決済みにしました",
                    }
                )
            else:
                raise HTTPException(status_code=404, detail="Feedback not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to resolve feedback: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to resolve feedback: {e}"
            )
