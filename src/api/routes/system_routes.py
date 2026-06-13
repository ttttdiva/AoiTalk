"""ヘルスチェック・管理者再起動・ランタイム機能フラグ系ルート (server.py から移設)"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ...bot.service import discord_bot_service
from ...runtime_features import runtime_feature_manager
from ..router_helpers import cookie_auth_dependency
from .payloads import RuntimeFeaturePatchPayload

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def runtime_feature_response() -> Dict[str, Any]:
    """Return runtime feature flags with live Discord bot service state."""
    status = runtime_feature_manager.status()
    status["discord_bot_service"] = discord_bot_service.status()
    return status


def register_system_routes(app: FastAPI, server: "WebChatServer") -> None:
    """health / admin restart / runtime features 系ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/health")
    async def health_check():
        """認証不要のヘルスチェック"""
        return JSONResponse({"status": "ok"})

    @app.get("/api/health")
    async def api_health_check():
        """認証不要のAPIヘルスチェック"""
        return JSONResponse({"status": "ok"})

    @app.post("/api/admin/restart")
    async def admin_restart(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 全プロセスを再起動（run.batのリスタートループ経由）"""
        is_admin = await server._is_admin_user(request)
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        logger.info("Admin restart requested — exiting with code 42")

        async def _restart():
            await asyncio.sleep(0.5)
            from src.service_manager import kill_services

            kill_services()
            # exit code 42 signals run.bat to restart all services
            os._exit(42)

        asyncio.create_task(_restart())
        return JSONResponse({"success": True, "message": "再起動します"})

    @app.get("/api/runtime/features")
    async def get_runtime_features():
        """Expose explicit runtime feature/adaptor state."""
        discord_bot_service.configure(server.config)
        return JSONResponse(runtime_feature_response())

    @app.patch("/api/runtime/features")
    async def update_runtime_feature(
        payload: RuntimeFeaturePatchPayload, _: None = Depends(require_auth)
    ):
        try:
            status = runtime_feature_manager.update_feature(
                payload.feature,
                payload.enabled,
                persist=True,
            )
            discord_bot_service.configure(server.config)
            if runtime_feature_manager.discord_enabled:
                await discord_bot_service.ensure_started(server.config)
            else:
                await discord_bot_service.stop()
            status = runtime_feature_response()
            definition = next(
                (
                    item
                    for item in status["definitions"]
                    if item["key"] == payload.feature
                ),
                None,
            )
            return JSONResponse(
                {
                    "status": "updated",
                    "restart_required": bool(definition and definition["restart_required"]),
                    **status,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
