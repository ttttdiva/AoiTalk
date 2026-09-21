"""
Heartbeat API Routes

Heartbeatの一覧取得・詳細・作成・更新・削除・手動トリガー・ステータスを提供する REST API。
"""
import logging
import re
import uuid
from typing import Any, Optional, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..heartbeat.security import (
    GENERIC_REQUEST_ERROR,
    HeartbeatSecurityError,
    validate_heartbeat_name,
)
from ..heartbeat.history import decode_history_cursor

logger = logging.getLogger(__name__)


# History is an operational surface.  Keep the response deliberately narrow
# instead of serializing the ORM object (which may contain an encrypted cursor
# or implementation details).  The runner/repository already applies the same
# allow-list when writing rows; this second boundary protects this API from
# accidental schema expansion.
_HISTORY_STRING_LIMIT = 512
_HISTORY_QUESTION_LIMIT = 16
_HISTORY_QUESTION_TEXT_LIMIT = 500
_HISTORY_UNSAFE_SUMMARY_RE = re.compile(
    r"(?:raw|full|source)?\s*(?:evidence|chat|docs?|tool|prompt|transcript|payload)"
    r"(?:\s*(?:body|text|output|arguments?|data))?",
    re.IGNORECASE,
)
_HISTORY_STATUS_ALIASES = {
    "running": "running",
    "succeeded": "succeeded",
    "success": "succeeded",
    "ok": "succeeded",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
    "stale": "stale",
}


def _isoformat(value: Any) -> str | None:
    """Serialize a datetime-like value without exposing ORM internals."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _safe_question(value: Any) -> dict[str, Any] | str | None:
    """Return a bounded, non-sensitive question summary.

    Project Steward history is intentionally not a transcript.  Keep only
    common summary fields and cap every value so a malformed/legacy row cannot
    turn the Settings page into a raw model-output dump.
    """
    def safe_text(item: Any) -> str:
        text = str(item)[:_HISTORY_QUESTION_TEXT_LIMIT]
        if re.search(
            r"(?:api[_ -]?key|access[_ -]?token|password|secret|bearer)\s*[:=]",
            text,
            re.IGNORECASE,
        ):
            return "[REDACTED]"
        return text

    if isinstance(value, str):
        return safe_text(value)
    if not isinstance(value, dict):
        return None
    allowed = ("question", "summary", "topic", "title", "message", "urgency", "status")
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, (str, int, float, bool)):
            result[key] = safe_text(item)
    return result or None


def _safe_history_dict(row: Any) -> dict[str, Any]:
    """Convert one HeartbeatRunHistory row to the public operational DTO."""
    def attr(name: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    def bounded_count(name: str, default: int = 0) -> int:
        try:
            return max(0, int(attr(name, default) or 0))
        except (TypeError, ValueError, OverflowError):
            return default

    questions_value = attr("questions_json", attr("questions", []))
    if isinstance(questions_value, (tuple, list)):
        questions = [
            item
            for item in (_safe_question(value) for value in questions_value[:_HISTORY_QUESTION_LIMIT])
            if item is not None
        ]
    else:
        questions = []

    summary = attr("result_summary", attr("summary"))
    if not isinstance(summary, str):
        summary = None
    if summary is not None:
        summary = str(summary)[:_HISTORY_STRING_LIMIT]
        if re.search(
            r"(?:api[_ -]?key|access[_ -]?token|password|secret|bearer)\s*[:=]",
            summary,
            re.IGNORECASE,
        ):
            summary = "[REDACTED]"
        elif _HISTORY_UNSAFE_SUMMARY_RE.search(summary):
            summary = None
    row_id = attr("id")
    raw_error_code = attr("safe_error_code")
    safe_error_code = (
        str(raw_error_code)[:64]
        if raw_error_code is not None
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", str(raw_error_code).strip().casefold())
        else None
    )
    return {
        "id": str(row_id) if row_id is not None else None,
        "heartbeat_name": str(attr("heartbeat_name", ""))[:120],
        "mode": str(attr("mode", ""))[:64] or None,
        "scope_type": str(attr("scope_type", "global"))[:32],
        "scope_id": str(attr("scope_id", "global"))[:120],
        "project_id": str(attr("project_id")) if attr("project_id") is not None else None,
        "started_at": _isoformat(attr("started_at")),
        "completed_at": _isoformat(attr("completed_at")),
        "status": _HISTORY_STATUS_ALIASES.get(
            str(attr("status", "unknown")).strip().casefold(),
            "failed",
        ),
        "success": (
            bool(attr("success"))
            if attr("success") is not None
            else None
        ),
        "memory_upsert_count": bounded_count("memory_upsert_count"),
        "forgotten_count": bounded_count("forgotten_count"),
        "question_count": max(bounded_count("question_count", len(questions)), len(questions)),
        "continuation_pending": bool(
            attr("continuation_pending", attr("continuation_state", False))
        ),
        "forced": bool(attr("forced", False)),
        "generic_action_count": bounded_count("generic_action_count"),
        "generic_action_failure_count": bounded_count(
            "generic_action_failure_count"
        ),
        "result_summary": summary,
        "questions": questions,
        "safe_error_code": safe_error_code,
    }


class CreateHeartbeatRequest(BaseModel):
    """Heartbeat作成リクエスト"""
    name: str
    description: str
    checklist: str
    interval_minutes: int = 30
    enabled: bool = True
    active_hours: Optional[dict] = None
    notify_channel: str = "websocket"


class UpdateHeartbeatRequest(BaseModel):
    """Heartbeat更新リクエスト"""
    description: Optional[str] = None
    checklist: Optional[str] = None
    interval_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    active_hours: Optional[dict] = None
    notify_channel: Optional[str] = None


def create_heartbeat_router(
    require_admin: Callable[..., Awaitable[None]],
) -> APIRouter:
    """Heartbeat API ルーターを作成（全エンドポイント管理者限定）"""
    router = APIRouter(prefix="/api/heartbeats", tags=["heartbeats"])

    @router.get("/status")
    async def get_runner_status(request: Request, _=Depends(require_admin)):
        """Runner全体のステータスを取得"""
        try:
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            return JSONResponse(content={"success": True, "status": runner.get_status()})
        except Exception:
            logger.exception("Heartbeatステータス取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.get("")
    async def list_heartbeats(request: Request, _=Depends(require_admin)):
        """全Heartbeat一覧を取得"""
        try:
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.runner import get_heartbeat_runner
            registry = get_heartbeat_registry()
            runner = get_heartbeat_runner()
            status = runner.get_status()

            heartbeats = []
            for h in registry.get_all():
                item = h.to_dict()
                item["last_result"] = status.get("last_results", {}).get(h.name)
                heartbeats.append(item)

            return JSONResponse(content={"success": True, "heartbeats": heartbeats})
        except Exception:
            logger.exception("Heartbeat一覧取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    async def _list_history(
        *,
        heartbeat_name: str | None,
        mode: str | None,
        project_id: str | None,
        status: str | None,
        limit: int,
        cursor: str | None,
    ) -> JSONResponse:
        """Read bounded Heartbeat operational history.

        This endpoint deliberately uses the history table rather than
        ``HeartbeatRunState``: scheduler state is a single mutable row per
        scope while Settings needs an append-only, restart-safe run log.
        Database initialization/schema failures are reported as 503 so an
        unavailable optional history store cannot break the existing CRUD and
        trigger APIs.
        """
        safe_name: str | None = None
        if heartbeat_name:
            try:
                safe_name = validate_heartbeat_name(heartbeat_name)
            except HeartbeatSecurityError:
                raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)

        parsed_project_id: uuid.UUID | None = None
        if project_id:
            try:
                parsed_project_id = uuid.UUID(project_id)
            except (TypeError, ValueError, AttributeError):
                raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)

        safe_mode: str | None = None
        if mode:
            safe_mode = mode.strip().casefold()
            if safe_mode not in {"agent_check", "project_steward"}:
                raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)

        safe_status: str | None = None
        if status:
            safe_status = status.strip().lower()
            safe_status = _HISTORY_STATUS_ALIASES.get(safe_status)
            if safe_status is None:
                raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)

        if cursor:
            try:
                decode_history_cursor(cursor)
            except ValueError:
                raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
            except ImportError:
                # The handler below reports a missing history deployment as
                # 503; leave validation to that boundary when unavailable.
                pass

        try:
            # The repository is the single history read boundary.  It applies
            # keyset pagination and projects only safe DTO fields; in
            # particular, encrypted scheduler cursors and raw error messages
            # never leave this process.
            from ..heartbeat.runner import get_heartbeat_runner

            page = await get_heartbeat_runner().list_history(
                heartbeat_name=safe_name,
                mode=safe_mode,
                project_id=parsed_project_id,
                statuses=[safe_status] if safe_status else None,
                cursor=cursor,
                limit=limit,
            )
            raw_page = page.to_dict() if hasattr(page, "to_dict") else page
            if isinstance(raw_page, dict):
                raw_items = raw_page.get("items", raw_page.get("runs", []))
                next_cursor = raw_page.get("next_cursor")
                has_more = bool(raw_page.get("has_more", False))
            else:
                raw_items = getattr(page, "items", getattr(page, "runs", []))
                next_cursor = getattr(page, "next_cursor", None)
                has_more = bool(getattr(page, "has_more", False))
            if not isinstance(raw_items, (list, tuple)):
                raw_items = []
            safe_items = [
                _safe_history_dict(item)
                for item in raw_items
                if item is not None
            ]
            # Re-project even a repository-provided ``to_dict`` result.  This
            # keeps a transitional adapter or future ORM field from turning
            # the admin endpoint into a raw transcript/error surface.
            payload = {
                "items": safe_items,
                "runs": list(safe_items),
                "next_cursor": next_cursor if isinstance(next_cursor, str) else None,
                "has_more": has_more,
            }
            payload["success"] = True
            # Keep request metadata explicit for clients that want to render a
            # page-size indicator without coupling to the repository DTO.
            payload.setdefault("limit", limit)
            payload.setdefault("cursor", cursor)
            return JSONResponse(content=payload)
        except HTTPException:
            raise
        except (ImportError, AttributeError, RuntimeError) as exc:
            # Missing migration/model/database initialization is an operational
            # unavailability, not a caller error.  Do not expose internals.
            logger.warning("Heartbeat history unavailable: %s", exc)
            raise HTTPException(status_code=503, detail=GENERIC_REQUEST_ERROR)
        except Exception:
            logger.exception("Heartbeat history retrieval error")
            raise HTTPException(status_code=503, detail=GENERIC_REQUEST_ERROR)

    @router.get("/history")
    async def list_heartbeat_history(
        request: Request,
        heartbeat_name: Optional[str] = Query(None, max_length=120),
        project_id: Optional[str] = Query(None, max_length=120),
        status: Optional[str] = Query(None, max_length=32),
        mode: Optional[str] = Query(None, max_length=32),
        limit: int = Query(25, ge=1, le=100),
        cursor: Optional[str] = Query(None, max_length=512),
        _=Depends(require_admin),
    ):
        """Recent bounded execution history for the Heartbeats Settings page."""
        return await _list_history(
            heartbeat_name=heartbeat_name,
            mode=mode,
            project_id=project_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @router.get("/{name}/history")
    async def list_named_heartbeat_history(
        name: str,
        request: Request,
        project_id: Optional[str] = Query(None, max_length=120),
        status: Optional[str] = Query(None, max_length=32),
        mode: Optional[str] = Query(None, max_length=32),
        limit: int = Query(25, ge=1, le=100),
        cursor: Optional[str] = Query(None, max_length=512),
        _=Depends(require_admin),
    ):
        """Compatibility alias for clients that scope history by path name."""
        return await _list_history(
            heartbeat_name=name,
            mode=mode,
            project_id=project_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @router.get("/{name}")
    async def get_heartbeat(name: str, request: Request, _=Depends(require_admin)):
        """Heartbeat詳細を取得"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.runner import get_heartbeat_runner
            registry = get_heartbeat_registry()
            heartbeat = registry.get(safe_name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            runner = get_heartbeat_runner()
            result = heartbeat.to_dict()
            result["last_result"] = runner.get_status().get("last_results", {}).get(safe_name)
            return JSONResponse(content={"success": True, "heartbeat": result})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat取得エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.post("")
    async def create_heartbeat(
        req: CreateHeartbeatRequest,
        request: Request,
        _=Depends(require_admin),
    ):
        """新しいHeartbeatを作成（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(req.name)
            from ..heartbeat.models import HeartbeatDefinition
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            if safe_name in registry:
                raise HTTPException(status_code=409, detail=GENERIC_REQUEST_ERROR)

            heartbeat = HeartbeatDefinition(
                name=safe_name,
                description=req.description,
                checklist=req.checklist,
                interval_minutes=req.interval_minutes,
                enabled=req.enabled,
                active_hours=req.active_hours,
                notify_channel=req.notify_channel,
                actions=[],
            )

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()}, status_code=201)
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat作成エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.put("/{name}")
    async def update_heartbeat(
        name: str,
        req: UpdateHeartbeatRequest,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを更新（管理者のみ。actions は YAML 管理のまま保持）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry, register_heartbeat
            from ..heartbeat.loader import save_heartbeat_to_yaml

            registry = get_heartbeat_registry()
            heartbeat = registry.get(safe_name)
            if not heartbeat:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            if req.description is not None:
                heartbeat.description = req.description
            if req.checklist is not None:
                heartbeat.checklist = req.checklist
            if req.interval_minutes is not None:
                heartbeat.interval_minutes = req.interval_minutes
            if req.enabled is not None:
                heartbeat.enabled = req.enabled
            if "active_hours" in req.model_fields_set:
                heartbeat.active_hours = req.active_hours
            if req.notify_channel is not None:
                heartbeat.notify_channel = req.notify_channel

            if not save_heartbeat_to_yaml(heartbeat):
                raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

            registry.unregister(safe_name)
            register_heartbeat(heartbeat)
            return JSONResponse(content={"success": True, "heartbeat": heartbeat.to_dict()})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat更新エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.delete("/{name}")
    async def delete_heartbeat(
        name: str,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを削除（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.registry import get_heartbeat_registry
            from ..heartbeat.loader import delete_heartbeat_yaml

            registry = get_heartbeat_registry()
            if safe_name not in registry:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)

            registry.unregister(safe_name)
            delete_heartbeat_yaml(safe_name)
            return JSONResponse(content={"success": True, "message": "Heartbeat を削除しました"})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeat削除エラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    @router.post("/{name}/trigger")
    async def trigger_heartbeat(
        name: str,
        request: Request,
        _=Depends(require_admin),
    ):
        """Heartbeatを手動で即時実行（管理者のみ）"""
        try:
            safe_name = validate_heartbeat_name(name)
            from ..heartbeat.runner import get_heartbeat_runner
            runner = get_heartbeat_runner()
            result = await runner.trigger(safe_name)
            if result is None:
                raise HTTPException(status_code=404, detail=GENERIC_REQUEST_ERROR)
            return JSONResponse(content={"success": True, "result": result})
        except HeartbeatSecurityError:
            raise HTTPException(status_code=400, detail=GENERIC_REQUEST_ERROR)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Heartbeatトリガーエラー")
            raise HTTPException(status_code=500, detail=GENERIC_REQUEST_ERROR)

    return router
