"""生成メディア配信 API。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ...services.generated_media_service import (
    get_media_record,
    resolve_media_file,
    user_can_access_media,
)
from ..router_helpers import cookie_auth_dependency

logger = logging.getLogger(__name__)


def build_generated_media_router(
    enforce_cookie_auth: Callable[..., Any],
    get_user_id: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["generated-media"])
    require_auth = cookie_auth_dependency(enforce_cookie_auth)

    @router.get("/api/generated-media/{media_id}")
    async def serve_generated_media(
        media_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        user_info = await get_user_id(request)
        user_id = ""
        if isinstance(user_info, dict):
            user_id = str(user_info.get("id") or user_info.get("user_id") or "")
        elif user_info is not None:
            user_id = str(getattr(user_info, "id", "") or getattr(user_info, "user_id", ""))

        media = await get_media_record(media_id)
        if media is None or media.status != "succeeded":
            raise HTTPException(status_code=404, detail="画像が見つかりません")

        if not await user_can_access_media(user_id, media):
            raise HTTPException(status_code=403, detail="Access denied")

        file_path = resolve_media_file(media)
        if file_path is None:
            raise HTTPException(status_code=404, detail="画像が見つかりません")

        return FileResponse(
            path=str(file_path),
            media_type=media.mime_type or "application/octet-stream",
        )

    return router
