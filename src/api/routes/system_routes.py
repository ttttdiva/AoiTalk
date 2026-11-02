"""ヘルスチェック・管理者再起動・ランタイム機能フラグ系ルート (server.py から移設)"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError
from sqlalchemy import select, text
from starlette.background import BackgroundTask

from ...bot.service import discord_bot_service
from ...features import Features
from ...restart_watchdog import launch_restart_watchdog
from ...service_manager._qdrant_server import managed_qdrant_status
from ...services import local_browser
from ...runtime_features import (
    RuntimeFeatureRollbackError,
    runtime_feature_coordinator,
    runtime_feature_manager,
)
from ...services.graceful_shutdown import GracefulShutdownError, GracefulShutdownGate
from ...services.restart_drain import RestartDrainError, restart_drain
from ...services.admin_restart_admission import (
    active_chat_requests,
    release_admin_restart,
    reserve_admin_restart,
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
_ADMIN_RESTART_BUSY_CHECK_TIMEOUT_SECONDS = 5.0


def _storage_fingerprint(database: str, schema: str) -> str:
    """Return an opaque identity for the active PostgreSQL connection.

    The probe intentionally never returns either value.  A live E2E harness
    supplies the expected fingerprint and receives only a boolean match.
    """
    # Keep PostgreSQL's case-sensitive quoted identifiers distinct.  The
    # caller's expected fingerprint is derived from the exact URL/schema text.
    canonical = f"{database.strip()}\x00{schema.strip()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _ShutdownPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_boot_id: StrictStr
    operation_id: StrictStr


class _RestartDrainPayload(_ShutdownPayload):
    timeout_seconds: float | None = None




def _task_is_active(task: Any) -> bool:
    done = getattr(task, "done", None)
    # An object that cannot report completion is treated as active.
    return not callable(done) or not bool(done())


def _is_background_worker_session(server: Any, session_id: Any) -> bool:
    key = str(session_id or "").strip()
    if not key:
        return False
    sessions = getattr(server, "_background_worker_session_ids", None)
    return isinstance(sessions, set) and key in sessions


def _task_is_background_worker(server: Any, task: Any) -> bool:
    return _is_background_worker_session(
        server, getattr(task, "_conversation_session_id", None)
    )


# A generation or chat dispatch with no progress for this long is stuck, not
# working: a restart drain does not wait for it (2026-10-30 13:07, an AoiDots
# run frozen in running held the drain until its timeout).
_STALLED_WORK_SECONDS = 300.0


def _is_stalled(last_progress: datetime | None) -> bool:
    return last_progress is not None and datetime.utcnow() - last_progress > timedelta(
        seconds=_STALLED_WORK_SECONDS
    )


def _generation_is_stalled(server: Any, key: Any) -> bool:
    store = getattr(server, "_conversation_generation_status", None)
    status = store.get(key) if isinstance(store, dict) else None
    updated = status.get("updated_at") if isinstance(status, dict) else None
    try:
        last_progress = datetime.fromisoformat(str(updated).removesuffix("Z")) if updated else None
    except ValueError:
        return False
    return _is_stalled(last_progress)


def _inflight_reasons(server: Any) -> list[str] | None:
    """Read-only look at in-process work; ``None`` means state is unknown."""
    reasons: list[str] = []
    try:
        dispatch = server._conversation_dispatch_tasks
        generation = server._conversation_generation_tasks
        late = server._conversation_late_finalize_tasks
        if not isinstance(dispatch, (set, list, tuple)) or not isinstance(generation, dict):
            return None
        if not isinstance(late, (set, list, tuple)):
            return None
        if any(
            _task_is_active(task) and not _task_is_background_worker(server, task)
            for task in list(dispatch)
        ):
            reasons.append("conversation_dispatch")
        if any(
            _task_is_active(task)
            for session_id, tasks in list(generation.items())
            if not _is_background_worker_session(server, session_id)
            and not _generation_is_stalled(server, session_id)
            for task in list(tasks)
        ):
            reasons.append("conversation_generation")
        if any(
            _task_is_active(task) and not _task_is_background_worker(server, task)
            for task in active_chat_requests(server)
        ):
            reasons.append("admin_restart_chat_active")
        if any(
            _task_is_active(task) and not _task_is_background_worker(server, task)
            for task in list(late)
        ):
            reasons.append("conversation_late_finalize")

        # AgentWork is preemptible by default: shutdown requeues it without
        # spending an attempt.  Only open non-preemptible sections (external
        # sends, partial writes, billing, migrations) hold a shutdown.
        from ...services import work_preemption

        if work_preemption.active_sections():
            reasons.append("agent_work_non_preemptible")

        from ...services import gpu_admission

        with gpu_admission._registry_lock:
            gpu = list(gpu_admission._registry.values())
        for admission in gpu:
            with admission.lock:
                if (
                    admission.phase != "OPEN"
                    or admission.jobs
                    or admission.requests
                    or admission.spawns
                    or admission.lease is not None
                    or admission.manifest
                ):
                    reasons.append("gpu_admission_active")
                    break
    except Exception:
        logger.warning("Shutdown in-process busy inspection failed", exc_info=True)
        return None
    return reasons


async def _db_active_reasons(server: Any) -> list[str] | None:
    """Inspect durable chat dispatch work; ``None`` means state is unknown.

    Historical ``uncertain`` AgentRun rows (quarantined remote continuations)
    and admission-only queued stubs are deliberately not counted: they are not
    executing work and must never be resent or cancelled here.
    """
    manager = getattr(server, "_db_manager", None)
    if manager is None or not callable(getattr(manager, "get_session", None)):
        return None
    from ...memory.models import AgentRun, ConversationDispatchOutbox, ConversationSession

    # Running and claimable AgentWork rows are not counted: preemptible work
    # is requeued at shutdown, and the non-preemptible part is observed
    # in-process through its open sections.
    queries = [
        (
            "db_dispatch_outbox",
            ConversationDispatchOutbox.run_id,
            ConversationDispatchOutbox.status.in_(("pending", "claimed")),
        ),
    ]
    session = None
    reasons: list[str] = []
    try:
        session = await manager.get_session()
        for reason, column, condition in queries:
            statement = (
                select(
                    column,
                    ConversationSession.context,
                    ConversationDispatchOutbox.created_at,
                    AgentRun.updated_at,
                    AgentRun.last_event_at,
                )
                .outerjoin(
                    ConversationSession,
                    ConversationSession.id == ConversationDispatchOutbox.session_id,
                )
                .outerjoin(AgentRun, AgentRun.id == ConversationDispatchOutbox.run_id)
                .where(condition)
            )
            result = await session.execute(statement)
            active_rows = result.all()
            blocking_rows = [
                run_id
                for run_id, context, *moments in active_rows
                if not (
                    isinstance(context, dict)
                    and context.get("background_worker") is True
                )
                # The run's last event (or the row itself) shows progress.
                and not _is_stalled(max((m for m in moments if m is not None), default=None))
            ]
            if blocking_rows:
                reasons.append(reason)
    except Exception:
        logger.warning("Shutdown DB busy inspection failed", exc_info=True)
        return None
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                logger.debug("Shutdown busy inspection session close failed")
    return reasons


async def _collect_shutdown_busy_reasons(server: Any) -> list[str] | None:
    """Private read-only collector; never stops or cancels anything."""
    try:
        inflight = _inflight_reasons(server)
        if inflight is None:
            return None
        durable = await _db_active_reasons(server)
        if durable is None:
            return None
        return inflight + durable
    except Exception:
        logger.warning("Shutdown busy observation unavailable")
        return None


async def _drain_waiting_items(_server: Any, reasons: list[str]) -> list[dict[str, Any]]:
    """Describe what a restart reservation is waiting for (read-only)."""
    from ...services import work_preemption

    items: list[dict[str, Any]] = []
    if "agent_work_non_preemptible" in reasons:
        items.extend({"kind": "non_preemptible", **section} for section in work_preemption.active_sections())
    covered = {"agent_work_non_preemptible"} if items else set()
    return items + [{"kind": reason} for reason in reasons if reason not in covered]


async def _release_embedding_runtime() -> None:
    """Free the private embedding worker's GPU memory before a graceful exit.

    Busy requests are never cancelled; the worker's own atexit close still
    stops it once the process ends.
    """
    module = sys.modules.get("src.rag.embedding")
    runtime = module.peek_embedding_runtime() if module is not None else None
    if runtime is None:
        return
    try:
        result = await runtime.release()
        logger.info("Embedding runtime release before shutdown: %s", result.get("reason"))
    except Exception:
        logger.warning("Embedding runtime release before shutdown failed", exc_info=True)


def _request_graceful_exit(server: Any) -> None:
    binding = getattr(server, "_graceful_shutdown_binding", None)
    if binding is None:
        raise RuntimeError("graceful shutdown binding is unavailable")
    binding.request_exit()


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
    return _with_browser_health(payload)


def _with_browser_health(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Expose only the process-local, no-I/O browser status projection."""
    if local_browser.is_enabled():
        payload["browser"] = local_browser.status().to_dict()
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
        from ...services.free_team_service import openai_usage_actual_health_status

        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                _with_qdrant_health(
                    {
                        "status": "degraded",
                        "feedback_migration": migration_status,
                        "boot_id": BOOT_ID,
                        "openai_usage_actual": openai_usage_actual_health_status(),
                    }
                ),
                status_code=503,
            )
        return JSONResponse(
            _with_qdrant_health(
                {
                    "status": "ok",
                    "boot_id": BOOT_ID,
                    "openai_usage_actual": openai_usage_actual_health_status(),
                }
            )
        )

    @app.get("/api/health")
    async def api_health_check():
        """認証不要のAPIヘルスチェック"""
        from ...services.free_team_service import openai_usage_actual_health_status

        migration_status = getattr(server, "_feedback_migration_status", "ready")
        if Features.is_enterprise() and migration_status != "ready":
            return JSONResponse(
                _with_qdrant_health(
                    {
                        "status": "degraded",
                        "feedback_migration": migration_status,
                        "boot_id": BOOT_ID,
                        "openai_usage_actual": openai_usage_actual_health_status(),
                    }
                ),
                status_code=503,
            )
        return JSONResponse(
            _with_qdrant_health(
                {
                    "status": "ok",
                    "boot_id": BOOT_ID,
                    "openai_usage_actual": openai_usage_actual_health_status(),
                }
            )
        )

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

    @app.post(
        "/api/admin/restart",
        responses={
            409: {"description": "Restart was rejected because Chat is active or a restart is pending"},
            503: {"description": "Restart was rejected because busy state could not be verified"},
        },
    )
    async def admin_restart(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 全プロセスを再起動（run.batのリスタートループ経由）"""
        is_admin = await server._is_admin_user(request)
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin only")

        # Reserve before awaiting the read-only snapshot so repeated admin
        # clicks cannot schedule multiple process exits.
        if not reserve_admin_restart(server):
            return JSONResponse(
                {
                    "success": False,
                    "error": "restart_pending",
                    "message": "再起動要求を確認中です。しばらく待ってから状態を確認してください。",
                },
                status_code=409,
            )
        try:
            busy_reasons = await asyncio.wait_for(
                _collect_shutdown_busy_reasons(server),
                timeout=_ADMIN_RESTART_BUSY_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            release_admin_restart(server)
            logger.warning("Admin restart refused; busy inspection timed out")
            return JSONResponse(
                {
                    "success": False,
                    "error": "busy_state_timeout",
                    "message": "処理中の作業を確認できなかったため、再起動を保留しました。",
                },
                status_code=503,
            )
        except asyncio.CancelledError:
            release_admin_restart(server)
            raise
        except Exception:
            release_admin_restart(server)
            logger.warning("Admin restart refused; busy inspection failed", exc_info=True)
            return JSONResponse(
                {
                    "success": False,
                    "error": "busy_state_unknown",
                    "message": "処理中の作業を確認できなかったため、再起動を保留しました。",
                },
                status_code=503,
            )

        if busy_reasons is None:
            release_admin_restart(server)
            return JSONResponse(
                {
                    "success": False,
                    "error": "busy_state_unknown",
                    "message": "処理中の作業を確認できなかったため、再起動を保留しました。",
                },
                status_code=503,
            )
        if busy_reasons:
            release_admin_restart(server)
            logger.info("Admin restart refused; busy: %s", busy_reasons)
            return JSONResponse(
                {
                    "success": False,
                    "error": "operation_busy",
                    "busy_reasons": busy_reasons,
                    "message": "会話またはエージェント処理中のため再起動を保留しました。処理完了後に再試行してください。",
                },
                status_code=409,
            )

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

        restart_task = _restart()
        try:
            asyncio.create_task(restart_task)
        except RuntimeError:
            restart_task.close()
            release_admin_restart(server)
            logger.warning("Admin restart refused; event loop is unavailable")
            return JSONResponse(
                {"success": False, "error": "restart_unavailable"}, status_code=503
            )
        return JSONResponse({"success": True, "message": "再起動します"})

    shutdown_gate = GracefulShutdownGate(
        BOOT_ID, lambda: _request_graceful_exit(server)
    )

    def _shutdown_error(exc: GracefulShutdownError) -> JSONResponse:
        return JSONResponse(
            {"success": False, "error": exc.code}, status_code=exc.http_status
        )

    async def _dispatch_shutdown(operation_id: str) -> None:
        await _release_embedding_runtime()
        try:
            shutdown_gate.dispatch(operation_id)
        except GracefulShutdownError as exc:
            logger.error("Graceful shutdown dispatch failed: %s", exc.code)

    def _shutdown_supported() -> bool:
        binding = getattr(server, "_graceful_shutdown_binding", None)
        try:
            return binding is not None and binding.supported() is True
        except Exception:
            return False

    async def _drain_shutdown(operation_id: str) -> None:
        # The drain loop has just observed an idle state; the gate keeps this
        # one-shot and refuses a conflicting /api/admin/shutdown operation.
        shutdown_gate.prepare(
            expected_boot_id=BOOT_ID, operation_id=operation_id, busy_reasons=[]
        )
        await _release_embedding_runtime()
        shutdown_gate.dispatch(operation_id)

    async def _require_admin(request: Request) -> None:
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Admin only")

    def _drain_error(exc: RestartDrainError) -> JSONResponse:
        return JSONResponse(
            {"success": False, "error": exc.code}, status_code=exc.http_status
        )

    @app.get("/api/admin/restart-drain", include_in_schema=False)
    async def get_restart_drain(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 再起動予約の状態（待っている処理の数と内容）"""
        await _require_admin(request)
        return JSONResponse({"boot_id": BOOT_ID, **restart_drain.status()})

    @app.post("/api/admin/restart-drain", include_in_schema=False)
    async def reserve_restart_drain(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 再起動を予約する。新しいagent workを止め、処理が終わったら自分で正常終了する。"""
        await _require_admin(request)
        try:
            payload = _RestartDrainPayload.model_validate(await request.json())
            GracefulShutdownGate._validate_operation_id(payload.operation_id)
        except (ValueError, ValidationError, GracefulShutdownError):
            return JSONResponse(
                {"success": False, "error": "invalid_request"}, status_code=422
            )
        if payload.expected_boot_id != BOOT_ID:
            return JSONResponse({"success": False, "error": "boot_mismatch"}, status_code=409)
        if shutdown_gate.snapshot() is not None or not _shutdown_supported():
            return JSONResponse(
                {"success": False, "error": "shutdown_unavailable"}, status_code=503
            )
        try:
            status = restart_drain.reserve(
                operation_id=payload.operation_id,
                timeout_seconds=payload.timeout_seconds,
                collect_busy=lambda: _collect_shutdown_busy_reasons(server),
                list_waiting=lambda reasons: _drain_waiting_items(server, reasons),
                shutdown=_drain_shutdown,
            )
        except RestartDrainError as exc:
            return _drain_error(exc)
        return JSONResponse(
            {"success": True, "boot_id": BOOT_ID, **status}, status_code=202
        )

    @app.delete("/api/admin/restart-drain", include_in_schema=False)
    async def cancel_restart_drain(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 再起動予約を取り消し、止めていた処理の開始を再開する。"""
        await _require_admin(request)
        try:
            status = restart_drain.withdraw("operator_cancelled")
        except RestartDrainError as exc:
            return _drain_error(exc)
        return JSONResponse({"success": True, "boot_id": BOOT_ID, **status})

    @app.post("/api/admin/shutdown", include_in_schema=False)
    async def admin_shutdown(request: Request, _: None = Depends(require_auth)):
        """管理者限定: 管理された無処理窓での正常終了 (run.bat は exit 0 で終了)"""
        is_admin = await server._is_admin_user(request)
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        try:
            payload = _ShutdownPayload.model_validate(await request.json())
        except (ValueError, ValidationError):
            return JSONResponse(
                {"success": False, "error": "invalid_request"}, status_code=422
            )

        def prepare(busy_reasons):
            return shutdown_gate.prepare(
                expected_boot_id=payload.expected_boot_id,
                operation_id=payload.operation_id,
                busy_reasons=busy_reasons,
            )

        # With an unknown busy state the gate still resolves stale boot,
        # malformed keys, replays and key conflicts first.
        try:
            receipt = prepare(None)
        except GracefulShutdownError as exc:
            if exc.code != "busy_state_unknown":
                return _shutdown_error(exc)
        else:
            return JSONResponse(
                {"success": receipt.state != "failed", **receipt.to_dict()}
            )

        if not _shutdown_supported():
            return JSONResponse(
                {"success": False, "error": "shutdown_unavailable"}, status_code=503
            )
        busy_reasons = await _collect_shutdown_busy_reasons(server)
        try:
            receipt = prepare(busy_reasons)
        except GracefulShutdownError as exc:
            if exc.code == "operation_busy":
                logger.info("Graceful shutdown refused; busy: %s", busy_reasons)
            return _shutdown_error(exc)
        logger.info("Graceful shutdown accepted for operation %s", receipt.operation_id)
        return JSONResponse(
            {"success": True, **receipt.to_dict()},
            background=BackgroundTask(_dispatch_shutdown, receipt.operation_id),
        )

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
