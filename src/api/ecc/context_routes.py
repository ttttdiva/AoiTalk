"""コンテキストプレビュールート。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...services.context_builder import ContextBuilder
from .schemas import ContextBuildPreviewRequest

logger = logging.getLogger(__name__)


def build_context_router(
    require_auth: Callable[..., Any],
    get_user_id: Callable[[Request], Any],
) -> APIRouter:
    """コンテキストプレビューの APIRouter を構築する。"""

    context_router = APIRouter(
        prefix="/api/context",
        tags=["context"],
    )

    @context_router.post("/build-preview")
    async def build_context_preview(
        payload: ContextBuildPreviewRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """LLMに渡す統合コンテキストをプレビューする。"""
        try:
            # The authenticated principal is authoritative. A preview payload
            # must never impersonate another user.
            user_id = await get_user_id(request)
            bundle = await ContextBuilder().build_context(
                user_id=user_id,
                message=payload.message,
                project_id=payload.project_id,
                task_id=payload.task_id,
                session_id=payload.session_id,
                max_chars=payload.max_chars,
            )
            if payload.project_id and bundle.debug.get("project_scope_authorized") is False:
                raise HTTPException(status_code=403, detail="project access denied")
            return JSONResponse(
                content={
                    "success": True,
                    "context": bundle.render_for_prompt(),
                    "debug": bundle.debug,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("コンテキストプレビュー生成エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return context_router
