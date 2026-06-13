"""フィードバック送信・一覧・解決ルート (server.py から移設)"""

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_feedback_routes(app: FastAPI, server: "WebChatServer") -> None:
    """フィードバック関連ルートを登録する (JSONL→DB 移行タスクの登録を含む)"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

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

        server._startup_background_tasks.append(_migrate_feedback)
    except ImportError as e:
        logger.warning(f"Feedback module import failed: {e}")
        FEEDBACK_AVAILABLE = False
        FeedbackRequest = None
        save_feedback_async = None
        load_feedback_async = None
        mark_feedback_resolved_async = None

    @app.post("/api/feedback")
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

    @app.get("/api/feedback")
    async def get_feedback_list(
        include_resolved: bool = False,
        limit: int = 100,
        _: None = Depends(require_auth),
    ):
        """Get list of feedback entries (for admin review)"""
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

    @app.post("/api/feedback/{feedback_id}/resolve")
    async def resolve_feedback(feedback_id: str, _: None = Depends(require_auth)):
        """Mark a feedback entry as resolved"""
        if not FEEDBACK_AVAILABLE:
            raise HTTPException(
                status_code=503, detail="Feedback system is not available"
            )

        try:
            success = await mark_feedback_resolved_async(feedback_id)
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
