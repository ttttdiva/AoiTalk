"""ヘルスチェック・管理者再起動・ランタイム機能フラグ系ルート (server.py から移設)"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ...bot.service import discord_bot_service
from ...features import Features
from ...runtime_features import (
    RuntimeFeatureRollbackError,
    runtime_feature_coordinator,
    runtime_feature_manager,
)
from ..router_helpers import cookie_auth_dependency
from .payloads import RuntimeFeaturePatchPayload

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)
BOOT_ID = str(uuid4())


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
        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                {
                    "status": "degraded",
                    "feedback_migration": migration_status,
                    "boot_id": BOOT_ID,
                },
                status_code=503,
            )
        return JSONResponse({"status": "ok", "boot_id": BOOT_ID})

    @app.get("/api/health")
    async def api_health_check():
        """認証不要のAPIヘルスチェック"""
        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                {
                    "status": "degraded",
                    "feedback_migration": migration_status,
                    "boot_id": BOOT_ID,
                },
                status_code=503,
            )
        return JSONResponse({"status": "ok", "boot_id": BOOT_ID})

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
    async def get_runtime_features(_: None = Depends(require_auth)):
        """Expose explicit runtime feature/adaptor state."""
        discord_bot_service.configure(server.config)
        return JSONResponse(runtime_feature_response())

    @app.patch("/api/runtime/features")
    async def update_runtime_feature(
        payload: RuntimeFeaturePatchPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Admin only")
        changes = payload.changes()
        static_features = {
            "local_mic": "voice_input",
            "local_speaker": "tts_output",
            "tts": "tts_output",
            "discord_bot": "discord_bot",
            "discord_text": "discord_bot",
            "discord_vc_input": "discord_bot",
            "discord_vc_output": "discord_bot",
        }
        for feature, enabled in changes.items():
            static_feature = static_features.get(feature)
            if enabled and static_feature and not Features.is_enabled(static_feature):
                raise HTTPException(
                    status_code=403,
                    detail="Runtime feature is disabled in this Enterprise profile",
                )

        try:
            await runtime_feature_coordinator.update_features(
                changes,
                config=server.config,
                discord_service=discord_bot_service,
            )
            status = runtime_feature_response()
            definitions = {item["key"]: item for item in status["definitions"]}
            return JSONResponse(
                {
                    "status": "updated",
                    "restart_required": any(
                        bool(definitions.get(feature, {}).get("restart_required"))
                        for feature in changes
                    ),
                    **status,
                }
            )
        except RuntimeFeatureRollbackError as exc:
            logger.critical("Runtime feature rollback failed", exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Runtime feature update failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=(400 if isinstance(exc, ValueError) else 503),
                detail=f"ランタイム機能の変更に失敗しました: {exc}",
            ) from exc
