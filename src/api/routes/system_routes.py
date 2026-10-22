"""ヘルスチェック・管理者再起動・ランタイム機能フラグ系ルート (server.py から移設)"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...bot.service import discord_bot_service
from ...features import Features
from ...restart_watchdog import launch_restart_watchdog
from ...service_manager._qdrant_server import managed_qdrant_status
from ...runtime_features import (
    RuntimeFeatureRollbackError,
    runtime_feature_coordinator,
    runtime_feature_manager,
)
from ..router_helpers import cookie_auth_dependency
from .mobile_identity_routes import register_mobile_identity_routes
from .payloads import RuntimeFeaturePatchPayload

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)
BOOT_ID = str(uuid4())
_STORAGE_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_FINGERPRINT_HEADER = "x-aoitalk-storage-fingerprint"


def _storage_fingerprint(database: str, schema: str) -> str:
    """Return an opaque identity for the active PostgreSQL connection.

    The probe intentionally never returns either value.  A live E2E harness
    supplies the expected fingerprint and receives only a boolean match.
    """
    # Keep PostgreSQL's case-sensitive quoted identifiers distinct.  The
    # caller's expected fingerprint is derived from the exact URL/schema text.
    canonical = f"{database.strip()}\x00{schema.strip()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_feature_response() -> Dict[str, Any]:
    """Return runtime feature flags with live Discord bot service state."""
    status = runtime_feature_manager.status()
    status["discord_bot_service"] = discord_bot_service.status()
    # Application capability flags are distinct from the low-level runtime
    # adapter flags above.  Include the effective profile values so clients
    # can hide company/autonomous panels without treating missing keys as an
    # implicit allow.
    status["application_features"] = Features.get_all()
    return status


def _with_qdrant_health(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Expose managed Qdrant degradation without changing API liveness."""
    qdrant = managed_qdrant_status()
    if qdrant.get("mode") == "local-managed":
        payload["qdrant"] = qdrant
    return payload


def register_system_routes(app: FastAPI, server: "WebChatServer") -> None:
    """health / admin restart / runtime features 系ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    # Native mobile performs this unauthenticated proof before sending a JWT.
    # Keep it in the system route registrar so server.py remains the stable
    # composition root and the endpoint stays hidden from shared OpenAPI.
    register_mobile_identity_routes(app, server)

    @app.get("/health")
    async def health_check():
        """認証不要のヘルスチェック"""
        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                _with_qdrant_health(
                    {
                        "status": "degraded",
                        "feedback_migration": migration_status,
                        "boot_id": BOOT_ID,
                    }
                ),
                status_code=503,
            )
        return JSONResponse(_with_qdrant_health({"status": "ok", "boot_id": BOOT_ID}))

    @app.get("/api/health")
    async def api_health_check():
        """認証不要のAPIヘルスチェック"""
        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                _with_qdrant_health(
                    {
                        "status": "degraded",
                        "feedback_migration": migration_status,
                        "boot_id": BOOT_ID,
                    }
                ),
                status_code=503,
            )
        return JSONResponse(_with_qdrant_health({"status": "ok", "boot_id": BOOT_ID}))

    @app.get("/api/verification/storage", include_in_schema=False)
    async def verification_storage_probe(request: Request):
        """Confirm the actual FastAPI DB target without disclosing identity.

        This is a read-only, internal-authenticated handshake used by live E2E
        setup.  An absent/stale server, a DB mismatch, and an unavailable DB
        all fail closed; no connection details or credentials are returned.
        """
        configured_key = str(os.environ.get("INTERNAL_API_KEY", "") or "").strip()
        supplied_key = str(request.headers.get("x-internal-auth", "") or "").strip()
        if not configured_key or not supplied_key or not hmac.compare_digest(
            supplied_key, configured_key
        ):
            return JSONResponse({"verified": False}, status_code=404)

        expected = str(
            request.headers.get(_STORAGE_FINGERPRINT_HEADER, "") or ""
        ).strip().lower()
        if not _STORAGE_FINGERPRINT_RE.fullmatch(expected):
            return JSONResponse({"verified": False}, status_code=400)

        manager = getattr(server, "_db_manager", None)
        if manager is None or not callable(getattr(manager, "get_session", None)):
            return JSONResponse({"verified": False}, status_code=503)

        session = None
        try:
            session = await manager.get_session()
            result = await session.execute(
                text(
                    "select current_database() as database_name, "
                    "current_schema() as schema_name"
                )
            )
            row = result.first()
            if row is None:
                return JSONResponse({"verified": False}, status_code=503)
            database = getattr(row, "database_name", None)
            schema = getattr(row, "schema_name", None)
            if database is None or schema is None:
                database, schema = row[0], row[1]
            actual = _storage_fingerprint(str(database), str(schema))
        except Exception:
            return JSONResponse({"verified": False}, status_code=503)
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    logger.debug("Verification storage probe session close failed")

        if not hmac.compare_digest(actual, expected):
            return JSONResponse({"verified": False}, status_code=409)
        return JSONResponse({"verified": True}, status_code=200)

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

            project_root = Path(__file__).resolve().parents[3]
            if not launch_restart_watchdog(project_root):
                logger.warning("Failed to launch restart fallback watchdog")

            kill_services()
            # exit code 42 signals run.bat to restart all services. The
            # detached watchdog only relaunches run.bat when that launcher
            # never enters a fresh :start cycle.
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
