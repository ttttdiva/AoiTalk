"""Project Overview generation from active Project-scoped Context Memory."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from sqlalchemy import or_, select

from ..app_config_store import AppConfigSnapshotUnavailable
from ..memory.database import get_db_session
from ..memory.models import (
    ContextMemory,
    Project,
    ProjectOverview,
    ProjectOverviewRefreshJob,
)
from .project_automation_model import (
    ProjectAutomationRouteError,
    cleanup_project_automation_llm_client,
    create_project_overview_llm_client,
    diagnose_project_automation_client,
    diagnose_project_automation_route,
)
from .project_overview_schema import (
    ProjectOverviewLayoutError,
    build_project_memory_digest,
    build_project_memory_snapshot,
    empty_project_overview_layout,
    validate_project_overview_layout,
)

logger = logging.getLogger(__name__)

PROJECT_OVERVIEW_GENERATION_TIMEOUT_SECONDS = 90.0

_SAFE_TIMEOUT_ERROR = "overview_generation_timeout"
_SAFE_PROVIDER_ERROR = "overview_provider_error"
_SAFE_CONFIG_UNAVAILABLE = "config_unavailable"
_SAFE_JSON_ERROR = "overview_invalid_json"
_SAFE_VALIDATION_ERROR = "overview_layout_validation_failed"
_SAFE_SOURCE_CHANGED = "source_changed_during_generation"

_SAFE_OVERVIEW_ERROR_CODES = frozenset(
    {
        _SAFE_TIMEOUT_ERROR,
        _SAFE_PROVIDER_ERROR,
        _SAFE_CONFIG_UNAVAILABLE,
        _SAFE_JSON_ERROR,
        _SAFE_VALIDATION_ERROR,
        _SAFE_SOURCE_CHANGED,
        "project_not_found",
        "overview_refresh_failed",
        "overview_refresh_requeue_failed",
        "overview_refresh_invalid_state",
    }
)

_SAFE_OVERVIEW_STATUS_VALUES = frozenset(
    {"pending", "building", "fresh", "failed"}
)
_SAFE_REFRESH_JOB_STATUS_VALUES = frozenset(
    {"pending", "running", "completed", "failed"}
)
_SAFE_OVERVIEW_REFRESH_REASONS = frozenset(
    {
        "project_memory_changed",
        "manual_refresh",
        "overview_row_missing",
        _SAFE_SOURCE_CHANGED,
        "project_overview_refresh",
        "scoped_memory_changed",
    }
)

_SAFE_EXCEPTION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")

# Failure diagnostics are intentionally process-local.  The durable Overview
# row stores only a safe error code and the last-known-good payload; this cache
# gives an operator useful stage context while preserving that storage contract
# and naturally falls back to code-based inference after a restart.
_OVERVIEW_DIAGNOSTICS: dict[str, dict[str, Any]] = {}
_OVERVIEW_DIAGNOSTICS_GUARD = threading.Lock()
_MAX_OVERVIEW_DIAGNOSTICS = 256

_REFRESH_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()

SessionFactory = Callable[[], Awaitable[Any]]


def _safe_diagnostic_token(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip().casefold()
    if re.fullmatch(r"^[a-z0-9][a-z0-9_.:-]{0,127}$", normalized):
        return normalized
    return fallback


def _safe_optional_diagnostic_token(value: Any) -> str | None:
    """Keep durable diagnostic fields to bounded enum-like tokens only."""

    text = str(value or "").strip().casefold()
    if not text:
        return None
    if re.fullmatch(r"^[a-z0-9][a-z0-9_.:-]{0,127}$", text):
        return text
    return None


def _safe_persisted_error_code(value: Any) -> str | None:
    """Project Overview rows may predate the safe error-code contract."""

    if not str(value or "").strip():
        return None
    token = _safe_optional_diagnostic_token(value)
    if not token:
        return _SAFE_PROVIDER_ERROR
    if token in _SAFE_OVERVIEW_ERROR_CODES:
        return token
    # Unknown legacy values are classified generically rather than surfaced as
    # arbitrary provider text (which may include URLs, credentials, or bodies).
    return _SAFE_PROVIDER_ERROR


def _safe_allowed_diagnostic_token(
    value: Any,
    allowed: frozenset[str],
) -> str | None:
    token = _safe_optional_diagnostic_token(value)
    return token if token in allowed else None


def _safe_exception_type(value: Any) -> str | None:
    """Keep only an exception class token, never its message/traceback."""

    normalized = str(value or "").strip()
    if _SAFE_EXCEPTION_TYPE_RE.fullmatch(normalized):
        return normalized[-128:]
    return None


def _safe_http_status_value(value: Any) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    if 100 <= status <= 599:
        return status
    return None


def _safe_transport_origin(value: Any) -> str | None:
    """Keep only a redacted HTTP(S) scheme/host/port transport origin."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        scheme = str(parsed.scheme or "").strip().casefold()
        hostname = str(parsed.hostname or "").strip()
        if scheme not in {"http", "https"} or not hostname:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (
        f"[{hostname}]"
        if ":" in hostname and not hostname.startswith("[")
        else hostname
    )
    return f"{scheme}://{host}{f':{port}' if port is not None else ''}"


def _safe_route_fields(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "provider",
        "model",
        "mode",
        "inherit",
        "route_valid",
        "ok",
        "code",
        "stage",
        "base_url_origin",
        "proxy_mode",
        "proxy_source",
        "proxy_origin",
        "proxy_configured",
        "proxy_bypassed",
        "base_url_configured",
        "api_key_configured",
        "reasoning_effort_configured",
    ):
        if key not in value:
            continue
        item = value[key]
        if key in {"base_url_origin", "proxy_origin"}:
            origin = _safe_transport_origin(item)
            if origin:
                result[key] = origin
            continue
        if key in {
            "provider",
            "model",
            "mode",
            "code",
            "stage",
            "proxy_mode",
            "proxy_source",
        }:
            text = str(item or "").strip()
            if text:
                result[key] = text[:256]
        elif key in {
            "inherit",
            "route_valid",
            "ok",
            "base_url_configured",
            "api_key_configured",
            "reasoning_effort_configured",
            "proxy_configured",
            "proxy_bypassed",
        }:
            result[key] = bool(item)
    return result


def _failure_diagnostic_payload(
    *,
    project_id: uuid.UUID | str,
    safe_error: str,
    stage: str,
    diagnostic_code: str,
    route: Mapping[str, Any] | None = None,
    exception_type: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    payload = {
        "project_id": str(project_id),
        "stage": _safe_diagnostic_token(stage, "provider_generation"),
        "code": _safe_diagnostic_token(diagnostic_code, safe_error),
        "error_code": _safe_diagnostic_token(safe_error, _SAFE_PROVIDER_ERROR),
        "route": _safe_route_fields(route),
    }
    safe_type = _safe_exception_type(exception_type)
    if safe_type:
        # This field is retained for server-side logging/cache inspection only;
        # the public projection below deliberately removes it.
        payload["exception_type"] = safe_type
    safe_status = _safe_http_status_value(http_status)
    if safe_status is not None:
        payload["http_status"] = safe_status
    return payload


def _remember_overview_diagnostics(
    project_id: uuid.UUID | str,
    payload: Mapping[str, Any],
) -> None:
    key = str(project_id)
    with _OVERVIEW_DIAGNOSTICS_GUARD:
        _OVERVIEW_DIAGNOSTICS[key] = copy.deepcopy(dict(payload))
        if len(_OVERVIEW_DIAGNOSTICS) > _MAX_OVERVIEW_DIAGNOSTICS:
            oldest = next(iter(_OVERVIEW_DIAGNOSTICS), None)
            if oldest is not None and oldest != key:
                _OVERVIEW_DIAGNOSTICS.pop(oldest, None)


def _clear_overview_diagnostics(project_id: uuid.UUID | str) -> None:
    with _OVERVIEW_DIAGNOSTICS_GUARD:
        _OVERVIEW_DIAGNOSTICS.pop(str(project_id), None)


def _cached_overview_diagnostics(project_id: uuid.UUID | str) -> dict[str, Any] | None:
    with _OVERVIEW_DIAGNOSTICS_GUARD:
        value = _OVERVIEW_DIAGNOSTICS.get(str(project_id))
        return copy.deepcopy(value) if value is not None else None


def _public_failure_diagnostics(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    result = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key != "exception_type"
    }
    return result


class ProjectOverviewServiceError(RuntimeError):
    """Base Project Overview service failure."""


class ProjectOverviewNotFound(ProjectOverviewServiceError):
    """Project does not exist or is deleted."""


class ProjectOverviewGenerationError(ProjectOverviewServiceError):
    """Generation failed with a safe externally persistable error code."""

    def __init__(
        self,
        safe_error: str,
        *,
        stage: str = "provider_generation",
        diagnostic_code: str | None = None,
        route: Mapping[str, Any] | None = None,
        exception_type: str | None = None,
        http_status: int | None = None,
    ):
        self.safe_error = str(safe_error or _SAFE_PROVIDER_ERROR)[:500]
        self.stage = _safe_diagnostic_token(stage, "provider_generation")
        self.diagnostic_code = _safe_diagnostic_token(
            diagnostic_code or self.safe_error,
            self.safe_error,
        )
        self.route = _safe_route_fields(route)
        self.exception_type = _safe_exception_type(exception_type)
        self.http_status = _safe_http_status_value(http_status)
        super().__init__(self.safe_error)


def _safe_http_status(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(
            getattr(exc, "response", None),
            "status_code",
            None,
        ),
    ):
        status = _safe_http_status_value(candidate)
        if status is not None:
            return status
    return None


def _uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectOverviewServiceError("invalid project id") from exc


def _clean_actor(value: Any) -> str:
    actor = str(value or "").strip()
    if not actor:
        raise ProjectOverviewServiceError("requested_by is required")
    if "\x00" in actor:
        raise ProjectOverviewServiceError("requested_by is invalid")
    return actor[:120]


def _clean_reason(value: Any) -> str:
    reason = str(value or "").strip()
    if "\x00" in reason:
        raise ProjectOverviewServiceError("reason is invalid")
    return reason[:128]


def _value(item: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


async def _open_session(session_factory: SessionFactory | None = None) -> Any:
    factory = session_factory or get_db_session
    return await factory()


@asynccontextmanager
async def _project_refresh_lock(project_id: uuid.UUID):
    key = str(project_id)
    with _REFRESH_LOCKS_GUARD:
        lock, references = _REFRESH_LOCKS.get(key, (asyncio.Lock(), 0))
        _REFRESH_LOCKS[key] = (lock, references + 1)

    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _REFRESH_LOCKS_GUARD:
            current = _REFRESH_LOCKS.get(key)
            if current is None:
                return
            current_lock, references = current
            if current_lock is not lock:
                return
            if references <= 1:
                _REFRESH_LOCKS.pop(key, None)
            else:
                _REFRESH_LOCKS[key] = (lock, references - 1)


def _active_memory_statement(project_id: uuid.UUID):
    now = datetime.utcnow()
    return (
        select(ContextMemory)
        .where(
            ContextMemory.project_id == project_id,
            ContextMemory.scope_type == "project",
            ContextMemory.status == "active",
            or_(
                ContextMemory.expires_at.is_(None),
                ContextMemory.expires_at > now,
            ),
        )
        .order_by(ContextMemory.id.asc())
    )


async def _load_active_project_memories(
    session: Any,
    project_id: uuid.UUID,
) -> list[ContextMemory]:
    return list(
        (
            await session.execute(
                _active_memory_statement(project_id)
            )
        )
        .scalars()
        .all()
    )


async def _require_project(
    session: Any,
    project_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id)
    if lock:
        statement = statement.with_for_update()
    project = await session.scalar(statement)
    if project is None or project.deleted_at is not None:
        raise ProjectOverviewNotFound("project not found")
    return project


async def _get_overview(
    session: Any,
    project_id: uuid.UUID,
    *,
    lock: bool = False,
) -> ProjectOverview | None:
    statement = select(ProjectOverview).where(
        ProjectOverview.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _ensure_overview(
    session: Any,
    project_id: uuid.UUID,
    *,
    status: str = "pending",
) -> ProjectOverview:
    overview = await _get_overview(session, project_id, lock=True)
    if overview is not None:
        return overview

    overview = ProjectOverview(
        id=uuid.uuid4(),
        project_id=project_id,
        layout_json=empty_project_overview_layout(),
        source_digest=None,
        generated_at=None,
        generation_version=1,
        status=status,
        error_message=None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(overview)
    await session.flush()
    return overview


def _memory_prompt_record(memory: Mapping[str, Any] | Any) -> dict[str, Any]:
    memory_id = str(_value(memory, "id") or "")
    return {
        "id": memory_id,
        "type": str(_value(memory, "memory_type") or ""),
        "title": str(_value(memory, "title") or ""),
        "content": str(_value(memory, "content") or ""),
        "importance": int(_value(memory, "importance") or 0),
        "confidence": float(_value(memory, "confidence") or 0.0),
        "is_pinned": bool(_value(memory, "is_pinned") or False),
    }


def _ordered_generation_memories(
    memories: Iterable[Mapping[str, Any] | Any],
) -> list[dict[str, Any]]:
    source = list(memories)
    by_id = {
        str(_value(memory, "id") or ""): memory
        for memory in source
    }
    snapshot = build_project_memory_snapshot(source)
    result: list[dict[str, Any]] = []
    for row in snapshot:
        memory = by_id.get(str(row["id"]))
        if memory is None:
            continue
        result.append(_memory_prompt_record(memory))
    return result


def _generation_prompt(
    *,
    project_id: uuid.UUID,
    active_memories: Iterable[Mapping[str, Any] | Any],
) -> str:
    memories = _ordered_generation_memories(active_memories)
    input_json = json.dumps(
        memories,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return (
        "You generate the declarative Project Overview layout for AoiTalk.\n"
        "Return exactly one JSON object and no prose, markdown, code fence, "
        "tool call, CSS, HTML, Mermaid, script, color, or executable content.\n\n"
        "The JSON schema is closed and has schema_version=1.\n"
        "Top-level shape:\n"
        '{"schema_version":1,"sections":[],"graph":{"nodes":[],"edges":[]}}\n\n'
        "sections: at most 8 objects. Each object must contain:\n"
        "- optional id: stable opaque string used only as a declarative identity\n"
        '- kind: one of "highlight", "bullets", "cards", "timeline"\n'
        "- title: short plain text\n"
        '- emphasis: one of "normal", "primary", "warning", "critical"\n'
        "- columns: 1 or 2\n"
        '- density: one of "compact", "normal"\n'
        "- memory_ids: array containing only active Project Memory UUIDs "
        "from the supplied input\n\n"
        "graph may contain optional title: short plain text. "
        "Do not put presentation instructions in it.\n\n"
        "graph.nodes: at most 24 objects. Each node must contain:\n"
        "- id: a stable opaque string. It is NOT required to be a UUID. "
        "Keep graph IDs opaque; do not reinterpret or normalize them as UUIDs.\n"
        '- kind: one of "lead", "member", "stakeholder", "system", "other"\n'
        "- label: short plain text\n"
        "- subtitle: optional short plain text\n"
        "- memory_ids: one or more supplied active Project Memory UUIDs\n\n"
        "graph.edges: at most 48 objects. Each edge must contain:\n"
        "- optional id: stable opaque string\n"
        "- source and target: opaque node IDs that exist in graph.nodes\n"
        "- label: optional short plain text\n"
        "- memory_ids: one or more supplied active Project Memory UUIDs\n"
        "Every edge must have referenced-memory evidence shared by both "
        "endpoint nodes.\n\n"
        "Do not invent facts. Every displayed section, graph node, and graph "
        "relationship must be grounded in the supplied Project Memory. "
        "Omit unsupported structure rather than guessing.\n\n"
        f"Project identity: {project_id}\n"
        "Active Project Memory JSON:\n"
        f"{input_json}"
    )


def _strip_single_json_fence(text: str) -> str:
    clean = str(text or "").strip()
    if not clean.startswith("```"):
        return clean
    lines = clean.splitlines()
    if len(lines) < 3 or not lines[-1].strip().startswith("```"):
        return clean
    opening = lines[0].strip().casefold()
    if opening not in {"```", "```json"}:
        return clean
    return "\n".join(lines[1:-1]).strip()


def _parse_generated_json(text: str) -> dict[str, Any]:
    clean = _strip_single_json_fence(text)
    if not clean:
        raise ProjectOverviewGenerationError(
            _SAFE_JSON_ERROR,
            stage="response_parse",
            diagnostic_code="empty_response",
        )
    try:
        payload = json.loads(clean)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProjectOverviewGenerationError(
            _SAFE_JSON_ERROR,
            stage="response_parse",
            diagnostic_code="invalid_json",
            exception_type=type(exc).__name__,
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectOverviewGenerationError(
            _SAFE_JSON_ERROR,
            stage="response_parse",
            diagnostic_code="json_not_object",
        )
    return payload


async def _generate_text(client: Any, prompt: str) -> str:
    for name in (
        "generate_plain_text_async",
        "generate_response_async",
        "generate_async",
    ):
        method = getattr(client, name, None)
        if not callable(method):
            continue
        result = method(prompt)
        if inspect.isawaitable(result):
            result = await result
        text = str(result or "").strip()
        if text:
            return text

    method = getattr(client, "generate", None)
    if not callable(method):
        method = getattr(client, "generate_response", None)

    if callable(method):
        try:
            result = await asyncio.to_thread(method, prompt, stream=False)
        except TypeError:
            result = await asyncio.to_thread(method, prompt)
        text = str(result or "").strip()
        if text:
            return text

    raise ProjectOverviewGenerationError(
        _SAFE_PROVIDER_ERROR,
        stage="provider_generation",
        diagnostic_code="provider_response_unavailable",
    )


async def generate_project_overview(
    config: Any,
    *,
    project_id: str | uuid.UUID,
    active_memories: Iterable[Mapping[str, Any] | Any],
    timeout_seconds: float = PROJECT_OVERVIEW_GENERATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Generate and validate schema-v1 layout from active Project Memory."""

    project_uuid = _uuid(project_id)
    memories = list(active_memories)
    if not memories:
        return empty_project_overview_layout()

    # The worker/diagnostics path passes this explicit sentinel when the
    # DB-backed effective configuration cannot be read.  Never construct a
    # provider client from startup configuration (or an empty fallback) in
    # that case; persist a stable operator-safe failure instead.
    if isinstance(config, AppConfigSnapshotUnavailable):
        raise ProjectOverviewGenerationError(
            _SAFE_CONFIG_UNAVAILABLE,
            stage="config_snapshot",
            diagnostic_code=_SAFE_CONFIG_UNAVAILABLE,
        )

    route_diagnostic = diagnose_project_automation_route(config)
    logger.info(
        "Project Overview generation route: project_id=%s provider=%s model=%s mode=%s route_valid=%s",
        project_uuid,
        route_diagnostic.get("provider") or "<empty>",
        route_diagnostic.get("model") or "<empty>",
        route_diagnostic.get("mode") or "<empty>",
        bool(route_diagnostic.get("route_valid")),
    )

    client = None
    try:
        try:
            client = create_project_overview_llm_client(config)
        except ProjectAutomationRouteError as exc:
            diagnostic = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=_SAFE_PROVIDER_ERROR,
                stage=getattr(exc, "stage", "route_resolution"),
                diagnostic_code=getattr(exc, "code", "invalid_route"),
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            )
            logger.warning(
                "Project Overview generation failed: project_id=%s stage=%s code=%s provider=%s model=%s mode=%s exception_type=%s",
                project_uuid,
                diagnostic["stage"],
                diagnostic["code"],
                route_diagnostic.get("provider") or "<empty>",
                route_diagnostic.get("model") or "<empty>",
                route_diagnostic.get("mode") or "<empty>",
                type(exc).__name__,
            )
            raise ProjectOverviewGenerationError(
                _SAFE_PROVIDER_ERROR,
                stage=diagnostic["stage"],
                diagnostic_code=diagnostic["code"],
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            ) from exc
        except Exception as exc:
            diagnostic = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=_SAFE_PROVIDER_ERROR,
                stage="client_creation",
                diagnostic_code="client_creation_failed",
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            )
            logger.warning(
                "Project Overview generation failed: project_id=%s stage=%s code=%s provider=%s model=%s mode=%s exception_type=%s",
                project_uuid,
                diagnostic["stage"],
                diagnostic["code"],
                route_diagnostic.get("provider") or "<empty>",
                route_diagnostic.get("model") or "<empty>",
                route_diagnostic.get("mode") or "<empty>",
                type(exc).__name__,
            )
            raise ProjectOverviewGenerationError(
                _SAFE_PROVIDER_ERROR,
                stage="client_creation",
                diagnostic_code="client_creation_failed",
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            ) from exc

        constructed_transport = diagnose_project_automation_client(client)
        if constructed_transport:
            route_diagnostic = {
                **route_diagnostic,
                **constructed_transport,
            }
            logger.info(
                "Project Overview constructed transport: "
                "project_id=%s base_url_origin=%s proxy_mode=%s "
                "proxy_source=%s proxy_configured=%s proxy_bypassed=%s "
                "proxy_origin=%s",
                project_uuid,
                route_diagnostic.get("base_url_origin") or "<unknown>",
                route_diagnostic.get("proxy_mode") or "<unknown>",
                route_diagnostic.get("proxy_source") or "<none>",
                bool(route_diagnostic.get("proxy_configured")),
                bool(route_diagnostic.get("proxy_bypassed")),
                route_diagnostic.get("proxy_origin") or "<none>",
            )

        prompt = _generation_prompt(
            project_id=project_uuid,
            active_memories=memories,
        )
        try:
            raw = await asyncio.wait_for(
                _generate_text(client, prompt),
                timeout=max(0.1, float(timeout_seconds)),
            )
        except asyncio.TimeoutError as exc:
            raise ProjectOverviewGenerationError(
                _SAFE_TIMEOUT_ERROR,
                stage="provider_generation",
                diagnostic_code="provider_timeout",
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            ) from exc
        except ProjectOverviewGenerationError as exc:
            if not exc.route:
                exc.route = _safe_route_fields(route_diagnostic)
            if not exc.exception_type:
                exc.exception_type = _safe_exception_type(type(exc).__name__)
            logger.warning(
                "Project Overview generation failed: project_id=%s stage=%s code=%s provider=%s model=%s mode=%s exception_type=%s",
                project_uuid,
                exc.stage,
                exc.diagnostic_code,
                route_diagnostic.get("provider") or "<empty>",
                route_diagnostic.get("model") or "<empty>",
                route_diagnostic.get("mode") or "<empty>",
                exc.exception_type or "<none>",
            )
            raise
        except Exception as exc:
            http_status = _safe_http_status(exc)
            diagnostic_code = (
                f"provider_http_{http_status}"
                if http_status is not None
                else "provider_request_failed"
            )
            raise ProjectOverviewGenerationError(
                _SAFE_PROVIDER_ERROR,
                stage="provider_generation",
                diagnostic_code=diagnostic_code,
                route=route_diagnostic,
                exception_type=type(exc).__name__,
                http_status=http_status,
            ) from exc

        payload = _parse_generated_json(raw)
        try:
            return validate_project_overview_layout(
                payload,
                active_memories=memories,
            )
        except ProjectOverviewLayoutError as exc:
            raise ProjectOverviewGenerationError(
                _SAFE_VALIDATION_ERROR,
                stage="layout_validation",
                diagnostic_code="layout_validation_failed",
                route=route_diagnostic,
                exception_type=type(exc).__name__,
            ) from exc
    finally:
        if client is not None:
            try:
                await cleanup_project_automation_llm_client(client)
            except Exception as cleanup_exc:
                logger.warning(
                    "Project Overview ephemeral LLM cleanup failed: exception_type=%s",
                    type(cleanup_exc).__name__,
                )


async def build_project_overview(
    config: Any,
    *,
    project_id: str | uuid.UUID,
    active_memories: Iterable[Mapping[str, Any] | Any],
    timeout_seconds: float = PROJECT_OVERVIEW_GENERATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build a validated Overview layout without persisting it."""

    memories = list(active_memories)
    if not memories:
        return empty_project_overview_layout()
    return await generate_project_overview(
        config,
        project_id=project_id,
        active_memories=memories,
        timeout_seconds=timeout_seconds,
    )


async def enqueue_project_overview_refresh(
    project_id: str | uuid.UUID,
    requested_by: str,
    reason: str = "",
    *,
    session_factory: SessionFactory | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any] | None:
    """Best-effort durable refresh enqueue with one pending follow-up per Project.

    A pending request is reused. If a job is already running, one pending
    follow-up may coexist with it so writes that occur during generation are
    not lost when the running job completes.
    """

    try:
        project_uuid = _uuid(project_id)
        actor = _clean_actor(requested_by)
        clean_reason = _clean_reason(reason)

        async with _project_refresh_lock(project_uuid):
            session = await _open_session(session_factory)
            async with session:
                try:
                    await _require_project(session, project_uuid, lock=True)

                    pending = await session.scalar(
                        select(ProjectOverviewRefreshJob)
                        .where(
                            ProjectOverviewRefreshJob.project_id == project_uuid,
                            ProjectOverviewRefreshJob.status == "pending",
                        )
                        .order_by(
                            ProjectOverviewRefreshJob.created_at.asc(),
                            ProjectOverviewRefreshJob.id.asc(),
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if pending is not None:
                        if clean_reason and not pending.reason:
                            pending.reason = clean_reason
                            pending.updated_at = datetime.utcnow()
                            await session.commit()
                        return pending.to_dict()

                    job = ProjectOverviewRefreshJob(
                        id=uuid.uuid4(),
                        project_id=project_uuid,
                        requested_by=actor,
                        status="pending",
                        reason=clean_reason or None,
                        error_message=None,
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    session.add(job)
                    await session.commit()
                    await session.refresh(job)
                    return job.to_dict()
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    raise
    except Exception:
        if raise_on_error:
            raise
        logger.exception(
            "Failed to enqueue Project Overview refresh for project %s",
            project_id,
        )
        return None


async def _mark_building(
    project_id: uuid.UUID,
    *,
    session_factory: SessionFactory | None,
) -> tuple[list[ContextMemory], str]:
    session = await _open_session(session_factory)
    async with session:
        try:
            await _require_project(session, project_id, lock=True)
            overview = await _ensure_overview(
                session,
                project_id,
                status="building",
            )
            memories = await _load_active_project_memories(
                session,
                project_id,
            )
            digest = build_project_memory_digest(memories)

            overview.status = "building"
            overview.error_message = None
            overview.updated_at = datetime.utcnow()

            await session.commit()
            return memories, digest
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


async def _mark_failed(
    project_id: uuid.UUID,
    safe_error: str,
    *,
    session_factory: SessionFactory | None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session = await _open_session(session_factory)
    async with session:
        try:
            await _require_project(session, project_id, lock=True)
            overview = await _ensure_overview(
                session,
                project_id,
                status="failed",
            )

            # Last-known-good fields are deliberately untouched:
            # layout_json, source_digest, generated_at and generation_version.
            overview.status = "failed"
            overview.error_message = str(
                safe_error or _SAFE_PROVIDER_ERROR
            )[:500]
            overview.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(overview)
            result = overview.to_dict()
            if diagnostics:
                result["diagnostics"] = _public_failure_diagnostics(diagnostics)
            return result
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


async def _save_if_digest_unchanged(
    project_id: uuid.UUID,
    *,
    expected_digest: str,
    layout: dict[str, Any],
    session_factory: SessionFactory | None,
) -> tuple[dict[str, Any], bool]:
    session = await _open_session(session_factory)
    async with session:
        try:
            await _require_project(session, project_id, lock=True)
            overview = await _ensure_overview(
                session,
                project_id,
                status="building",
            )

            current_memories = await _load_active_project_memories(
                session,
                project_id,
            )
            current_digest = build_project_memory_digest(current_memories)

            if current_digest != expected_digest:
                # Never persist layout generated from a stale source snapshot.
                # Preserve all last-known-good generation fields.
                overview.status = "pending"
                overview.error_message = None
                overview.updated_at = datetime.utcnow()
                await session.commit()
                await session.refresh(overview)
                result = overview.to_dict()
                result["source_changed"] = True
                return result, True

            try:
                normalized = validate_project_overview_layout(
                    layout,
                    active_memories=current_memories,
                )
            except ProjectOverviewLayoutError as exc:
                raise ProjectOverviewGenerationError(
                    _SAFE_VALIDATION_ERROR,
                    stage="layout_validation",
                    diagnostic_code="layout_validation_failed",
                    exception_type=type(exc).__name__,
                ) from exc

            previous_generated_at = overview.generated_at
            previous_version = int(overview.generation_version or 1)

            overview.layout_json = normalized
            overview.source_digest = current_digest
            overview.generated_at = datetime.utcnow()
            overview.generation_version = (
                previous_version + 1
                if previous_generated_at is not None
                else 1
            )
            overview.status = "fresh"
            overview.error_message = None
            overview.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(overview)
            result = overview.to_dict()
            result["source_changed"] = False
            return result, False
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


_FAILURE_STAGE_BY_ERROR: dict[str, str] = {
    _SAFE_TIMEOUT_ERROR: "provider_generation",
    _SAFE_JSON_ERROR: "response_parse",
    _SAFE_VALIDATION_ERROR: "layout_validation",
    _SAFE_PROVIDER_ERROR: "provider_generation",
    _SAFE_CONFIG_UNAVAILABLE: "config_snapshot",
}


def _overview_error_action(error_code: str | None) -> str | None:
    code = str(error_code or "").strip().casefold()
    if not code:
        return None
    if code == _SAFE_TIMEOUT_ERROR:
        return "retry_refresh"
    if code == _SAFE_JSON_ERROR:
        return "retry_refresh"
    if code == _SAFE_VALIDATION_ERROR:
        return "retry_refresh"
    if code == _SAFE_PROVIDER_ERROR:
        return "check_project_automation_route"
    if code == _SAFE_CONFIG_UNAVAILABLE:
        return "reload_project_automation_config"
    return "retry_refresh"


def build_project_overview_diagnostics(
    config: Any,
    *,
    project_id: str | uuid.UUID,
    overview: Mapping[str, Any] | Any | None = None,
    latest_job: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """Build an operator-facing, secret-free Overview diagnostic snapshot.

    The persisted Overview intentionally retains only a stable safe error code.
    When the failure happened in this process, the cache contributes exact
    stage/code context; otherwise the stage is conservatively inferred from the
    persisted code and marked as historical. Only sanitized endpoint/proxy
    origins may be returned. The current effective route and the route observed
    at failure time are reported separately; API keys, credentials, URL
    paths/query strings, headers, exception messages and tracebacks are never
    returned.
    """

    overview_status = _safe_allowed_diagnostic_token(
        _value(overview, "status", "pending"),
        _SAFE_OVERVIEW_STATUS_VALUES,
    ) or "pending"
    config_unavailable = isinstance(config, AppConfigSnapshotUnavailable)
    route = {} if config_unavailable else diagnose_project_automation_route(config)
    error_code = (
        _SAFE_CONFIG_UNAVAILABLE
        if config_unavailable
        else _safe_persisted_error_code(
            _value(overview, "error_message", "")
        )
    )
    cached = _cached_overview_diagnostics(project_id)
    if config_unavailable:
        stage = "config_snapshot"
        diagnostic_code = _SAFE_CONFIG_UNAVAILABLE
        source = "config_snapshot"
    elif cached:
        stage = str(cached.get("stage") or "provider_generation")
        diagnostic_code = str(
            cached.get("code") or error_code or "unknown"
        )
        source = "current_process"
    else:
        stage = _FAILURE_STAGE_BY_ERROR.get(
            error_code or "",
            "unknown",
        )
        diagnostic_code = error_code or ("ok" if route.get("ok") else route.get("code"))
        source = "persisted_overview" if error_code else "current_route"

    failure_route = (
        _safe_route_fields(cached.get("route"))
        if cached and isinstance(cached.get("route"), Mapping)
        else {}
    )

    has_last_known_good = bool(
        _value(overview, "generated_at")
        and _value(overview, "source_digest")
    )
    result: dict[str, Any] = {
        "project_id": str(project_id),
        "status": overview_status,
        "error_code": error_code,
        "stage": _safe_diagnostic_token(stage, "unknown"),
        "code": _safe_diagnostic_token(diagnostic_code, "unknown"),
        "source": source,
        "retryable": bool(error_code),
        "action": _overview_error_action(error_code),
        "has_last_known_good": has_last_known_good,
        "route_healthy": False if config_unavailable else bool(route.get("ok")),
        "route": _safe_route_fields(route),
    }
    if failure_route:
        result["failure_route"] = failure_route
    if cached and _safe_http_status_value(cached.get("http_status")) is not None:
        result["http_status"] = int(cached["http_status"])

    # ``overview_provider_error`` rows can outlive a route change (for
    # example, a previous local llama.cpp endpoint being replaced with the
    # currently healthy OpenAI route).  In that case the safe action is a
    # durable retry, not asking an operator to repair an already-valid route.
    if error_code == _SAFE_PROVIDER_ERROR and bool(route.get("ok")):
        result["action"] = "retry_refresh"
        result["historical_failure"] = source != "current_process"
    if latest_job is not None:
        result["latest_job"] = {
            "status": _safe_allowed_diagnostic_token(
                _value(latest_job, "status", ""),
                _SAFE_REFRESH_JOB_STATUS_VALUES,
            )
            or "unknown",
            # Refresh reasons and legacy error messages are persisted data and
            # may contain URLs, provider bodies, or credentials.  Expose only
            # enum-like tokens; invalid values are intentionally omitted.
            "reason": _safe_allowed_diagnostic_token(
                _value(latest_job, "reason", ""),
                _SAFE_OVERVIEW_REFRESH_REASONS,
            ),
            "error_code": _safe_persisted_error_code(
                _value(latest_job, "error_message", "")
            ),
            "created_at": _value(latest_job, "created_at"),
            "started_at": _value(latest_job, "started_at"),
            "completed_at": _value(latest_job, "completed_at"),
        }
    return result


async def refresh_project_overview(
    project_id: str | uuid.UUID,
    requested_by: str,
    config: Any,
    *,
    reason: str = "project_memory_changed",
    timeout_seconds: float = PROJECT_OVERVIEW_GENERATION_TIMEOUT_SECONDS,
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Refresh Project Overview while preserving last-known-good state.

    Memory is read in an independent transaction. Generation never participates
    in a Scoped Memory write transaction, so generation or Overview persistence
    failure cannot roll back the Memory mutation that requested this refresh.
    """

    project_uuid = _uuid(project_id)
    actor = _clean_actor(requested_by)

    async with _project_refresh_lock(project_uuid):
        memories, starting_digest = await _mark_building(
            project_uuid,
            session_factory=session_factory,
        )

        try:
            # This branch intentionally does not construct an LLM client.
            if not memories:
                layout = empty_project_overview_layout()
            else:
                layout = await build_project_overview(
                    config,
                    project_id=project_uuid,
                    active_memories=memories,
                    timeout_seconds=timeout_seconds,
                )
        except ProjectOverviewGenerationError as exc:
            diagnostics = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=exc.safe_error,
                stage=exc.stage,
                diagnostic_code=exc.diagnostic_code,
                route=exc.route,
                exception_type=exc.exception_type,
                http_status=exc.http_status,
            )
            _remember_overview_diagnostics(project_uuid, diagnostics)
            result = await _mark_failed(
                project_uuid,
                exc.safe_error,
                session_factory=session_factory,
                diagnostics=diagnostics,
            )
            result["memory_count"] = len(memories)
            result["requeued"] = False
            return result
        except asyncio.TimeoutError:
            diagnostics = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=_SAFE_TIMEOUT_ERROR,
                stage="provider_generation",
                diagnostic_code="provider_timeout",
                exception_type="TimeoutError",
            )
            _remember_overview_diagnostics(project_uuid, diagnostics)
            result = await _mark_failed(
                project_uuid,
                _SAFE_TIMEOUT_ERROR,
                session_factory=session_factory,
                diagnostics=diagnostics,
            )
            result["memory_count"] = len(memories)
            result["requeued"] = False
            return result
        except Exception as exc:
            logger.error(
                "Unexpected Project Overview generation failure: project_id=%s stage=%s exception_type=%s",
                project_uuid,
                "provider_generation",
                type(exc).__name__,
            )
            diagnostics = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=_SAFE_PROVIDER_ERROR,
                stage="provider_generation",
                diagnostic_code="unexpected_generation_failure",
                exception_type=type(exc).__name__,
            )
            _remember_overview_diagnostics(project_uuid, diagnostics)
            result = await _mark_failed(
                project_uuid,
                _SAFE_PROVIDER_ERROR,
                session_factory=session_factory,
                diagnostics=diagnostics,
            )
            result["memory_count"] = len(memories)
            result["requeued"] = False
            return result

        try:
            result, source_changed = await _save_if_digest_unchanged(
                project_uuid,
                expected_digest=starting_digest,
                layout=layout,
                session_factory=session_factory,
            )
        except ProjectOverviewGenerationError as exc:
            diagnostics = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=exc.safe_error,
                stage=exc.stage,
                diagnostic_code=exc.diagnostic_code,
                route=exc.route,
                exception_type=exc.exception_type,
                http_status=exc.http_status,
            )
            _remember_overview_diagnostics(project_uuid, diagnostics)
            result = await _mark_failed(
                project_uuid,
                exc.safe_error,
                session_factory=session_factory,
                diagnostics=diagnostics,
            )
            result["memory_count"] = len(memories)
            result["requeued"] = False
            return result
        except Exception as exc:
            logger.error(
                "Project Overview persistence failed: project_id=%s stage=%s exception_type=%s",
                project_uuid,
                "persistence",
                type(exc).__name__,
            )
            diagnostics = _failure_diagnostic_payload(
                project_id=project_uuid,
                safe_error=_SAFE_PROVIDER_ERROR,
                stage="persistence",
                diagnostic_code="persistence_failed",
                exception_type=type(exc).__name__,
            )
            _remember_overview_diagnostics(project_uuid, diagnostics)
            result = await _mark_failed(
                project_uuid,
                _SAFE_PROVIDER_ERROR,
                session_factory=session_factory,
                diagnostics=diagnostics,
            )
            result["memory_count"] = len(memories)
            result["requeued"] = False
            return result

    if source_changed:
        _clear_overview_diagnostics(project_uuid)
        queued = await enqueue_project_overview_refresh(
            project_uuid,
            actor,
            _SAFE_SOURCE_CHANGED,
            session_factory=session_factory,
        )
        result["requeued"] = queued is not None
        result["memory_count"] = len(memories)
        return result

    result["requeued"] = False
    result["memory_count"] = len(memories)
    _clear_overview_diagnostics(project_uuid)
    return result


__all__ = [
    "PROJECT_OVERVIEW_GENERATION_TIMEOUT_SECONDS",
    "ProjectOverviewGenerationError",
    "ProjectOverviewNotFound",
    "ProjectOverviewServiceError",
    "build_project_overview_diagnostics",
    "build_project_overview",
    "enqueue_project_overview_refresh",
    "generate_project_overview",
    "refresh_project_overview",
]
