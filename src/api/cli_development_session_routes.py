"""Authenticated API for durable CLI development sessions.

The route module is intentionally a thin transport boundary.  Session
admission, Agent binding, provider execution, event cursors and cancellation
belong to ``CLIDevelopmentSessionService``; this module only validates the
HTTP shape, resolves the authenticated human owner and maps typed service
errors to stable responses.

The service is optional during rolling deployment.  Routes remain registered
when the feature is disabled so operators can inspect the effective
configuration, while mutating/read endpoints fail closed with ``409``.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..services.cli_development_sessions import cli_development_policy


CLIProvider = Literal[
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
    "muse-remote",
]
PermissionMode = Literal["native_default", "standard", "accept_edits"]
RoleContextMode = Literal["raw", "agent_role"]
CLISessionStatus = Literal[
    "ready",
    "queued",
    "running",
    "completed",
    "failed",
    "waiting_input",
    "waiting_approval",
    "cancelling",
    "disconnected",
    "interrupted",
    "cancelled",
]


CLICapability = Literal["command_execute", "workspace_read", "workspace_write"]


class CLIDevelopmentSessionCreateRequest(BaseModel):
    """Pinned route and workspace for one durable CLI continuation."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {
                    "title": "Local CLI session",
                    "properties": {
                        "provider": {
                            "enum": [
                                "antigravity-cli",
                                "claude-cli",
                                "codex-cli",
                                "grok-cli",
                            ]
                        }
                    },
                    "required": ["provider", "model", "working_directory"],
                },
                {
                    "title": "Muse remote session",
                    "properties": {"provider": {"const": "muse-remote"}},
                    "required": ["provider"],
                    "not": {
                        "anyOf": [
                            {"required": ["model"]},
                            {"required": ["working_directory"]},
                            {"required": ["effort"]},
                            {"required": ["permission_mode"]},
                            {"required": ["allowed_capabilities"]},
                        ]
                    },
                },
            ]
        },
    )

    agent_id: str = Field(..., min_length=36, max_length=36)
    agent_revision_id: str | None = Field(default=None, min_length=36, max_length=36)
    project_id: str | None = Field(default=None, min_length=36, max_length=36)
    provider: CLIProvider
    # Local providers require these fields at service admission.  Muse remote
    # has no local model or filesystem knobs, so the transport leaves them
    # absent and lets the provider-specific service branch validate the shape.
    model: str | None = Field(default=None, min_length=1, max_length=160)
    effort: str | None = Field(default=None, min_length=1, max_length=40)
    working_directory: str | None = Field(default=None, min_length=1, max_length=4096)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=255)
    permission_mode: PermissionMode | None = None
    # Omitted keeps the legacy full ceiling; an explicit list may only narrow it.
    allowed_capabilities: list[CLICapability] | None = Field(default=None, min_length=1, max_length=3)
    # Omitted/null keeps the AgentRevision default runtime; an explicit id
    # selects a server-validated ExecutionProfile (never a role change).
    execution_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    # Omitted remains the historical raw provider invocation.  The managed UI
    # must opt into Agent role delivery explicitly.
    role_context_mode: RoleContextMode | None = None

    @model_validator(mode="after")
    def validate_provider_shape(self) -> "CLIDevelopmentSessionCreateRequest":
        local_fields = {
            "model",
            "working_directory",
            "effort",
            "permission_mode",
            "allowed_capabilities",
        }
        if self.provider == "muse-remote":
            if self.model_fields_set.intersection(local_fields):
                raise ValueError("muse-remote forbids local execution fields")
        elif self.model is None or self.working_directory is None:
            raise ValueError("local providers require model and working_directory")
        return self


class CLIDevelopmentSessionMessageRequest(BaseModel):
    """One raw user turn.

    Do not normalize or trim ``message`` here.  Whitespace is part of the
    caller's durable input and the service computes its request fingerprint
    from the exact value.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=100_000)
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class CLIDevelopmentSessionCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursors: dict[str, str] = Field(default_factory=dict, max_length=200)
    session_ids: list[str] | None = Field(default=None, max_length=200)
    limit_per_session: int = Field(default=100, ge=1, le=500)


class _CLIResponse(BaseModel):
    """Shared response base that preserves service-owned safe projections."""

    model_config = ConfigDict(extra="allow")
    success: bool = True


class CLIDevelopmentSessionOutputChunk(BaseModel):
    """Bounded output slice attached to a ``cli.output`` event."""

    model_config = ConfigDict(extra="allow")

    assistant_message_id: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    start: int | None = None
    end: int | None = None
    text: str | None = None
    text_end: int | None = None
    text_truncated: bool | None = None
    cli_session_sequence: int | None = None
    error_code: str | None = None


class CLIDevelopmentSessionEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    source: Literal["run", "work"]
    run_id: str | None = None
    work_item_id: str | None = None
    sequence: int
    session_sequence: int | None = None
    event_type: str
    status: str | None = None
    message: str | None = None
    payload: CLIDevelopmentSessionOutputChunk = Field(default_factory=CLIDevelopmentSessionOutputChunk)
    created_at: str | None = None


class CLIDevelopmentSessionSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    owner_id: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    binding_id: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    working_directory: str | None = None
    role_context_mode: RoleContextMode = "raw"
    role_context_sha256: str | None = None
    execution_target: str | None = None
    local_filesystem: bool | None = None
    sandbox: str | None = None
    turn_status: str | None = None
    delivery: dict[str, Any] | None = None
    status: CLISessionStatus = "ready"
    native_state: str | None = None
    run_id: str | None = None
    work_item_id: str | None = None
    created: bool | None = None
    policy: dict[str, Any] | None = None
    run: dict[str, Any] | None = None
    work: dict[str, Any] | None = None


class CLIDevelopmentSessionEventPage(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    events: list[CLIDevelopmentSessionEvent] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class CLIDevelopmentSessionCollectError(BaseModel):
    code: str
    detail: str


class CLIDevelopmentSessionCollectItem(BaseModel):
    """Flat bounded session header joined with one event page."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    owner_id: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    binding_id: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    working_directory: str | None = None
    role_context_mode: RoleContextMode = "raw"
    role_context_sha256: str | None = None
    execution_target: str | None = None
    local_filesystem: bool | None = None
    sandbox: str | None = None
    turn_status: str | None = None
    delivery: dict[str, Any] | None = None
    status: CLISessionStatus | None = None
    native_state: str | None = None
    run_id: str | None = None
    work_item_id: str | None = None
    events: list[CLIDevelopmentSessionEvent] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    error: CLIDevelopmentSessionCollectError | None = None


class CLIDevelopmentSessionPolicyEffective(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    execution_lane: str = "disabled"
    allowed_roots: list[str] = Field(default_factory=list)
    allowed_owner_ids: list[str] = Field(default_factory=list)
    allowed_agent_ids: list[str] = Field(default_factory=list)
    default_permission_mode: PermissionMode | None = None
    allowed_permission_modes: list[PermissionMode] = Field(default_factory=list)
    approval_policy_source: str | None = None
    provider_cache_roots: dict[str, str] = Field(default_factory=dict)


class CLIDevelopmentSessionPolicyContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str = "windows"
    cwd: str | None = None
    owner_id: str | None = None
    windows_user_authority: bool = False
    sandbox: bool = False


class CLIDevelopmentSessionTrustedWindowsStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    allowed_roots: list[str] = Field(default_factory=list)
    default_permission_mode: PermissionMode | None = None


class CLIDevelopmentSessionStatusResponse(_CLIResponse):
    enabled: bool = False
    available: bool = False
    source: str | None = None
    effective: CLIDevelopmentSessionPolicyEffective | None = None
    policy: CLIDevelopmentSessionPolicyContext | None = None
    trusted_windows: CLIDevelopmentSessionTrustedWindowsStatus | None = None


class CLIDevelopmentSessionResponse(_CLIResponse):
    session: CLIDevelopmentSessionSummary | None = None
    session_id: str | None = None
    owner_id: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    binding_id: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    working_directory: str | None = None
    role_context_mode: RoleContextMode | None = None
    role_context_sha256: str | None = None
    execution_target: str | None = None
    local_filesystem: bool | None = None
    sandbox: str | None = None
    turn_status: str | None = None
    delivery: dict[str, Any] | None = None
    status: CLISessionStatus | None = None
    policy: dict[str, Any] | None = None
    native_state: str | None = None
    run_id: str | None = None
    work_item_id: str | None = None
    run: dict[str, Any] | None = None
    work: dict[str, Any] | None = None
    accepted: bool | None = None
    created: bool | None = None


class CLIDevelopmentSessionListResponse(_CLIResponse):
    sessions: list[CLIDevelopmentSessionSummary] = Field(default_factory=list)


class CLIDevelopmentSessionCollectResponse(_CLIResponse):
    sessions: list[CLIDevelopmentSessionCollectItem] = Field(default_factory=list)
    items: list[CLIDevelopmentSessionCollectItem] = Field(default_factory=list)


class CLIDevelopmentSessionEventsResponse(_CLIResponse):
    session_id: str | None = None
    events: list[CLIDevelopmentSessionEvent] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool | None = None


class CLIDevelopmentSessionActionResponse(_CLIResponse):
    session: CLIDevelopmentSessionSummary | None = None
    session_id: str | None = None
    run_id: str | None = None
    work_item_id: str | None = None
    status: CLISessionStatus | None = None
    execution_target: str | None = None
    local_filesystem: bool | None = None
    sandbox: str | None = None
    turn_status: str | None = None
    delivery: dict[str, Any] | None = None
    cancel_requested: bool | None = None
    accepted: bool = True


class CLIDevelopmentSessionRouteError(RuntimeError):
    """Stable route-side error used when the optional service is unavailable."""

    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.http_status = int(http_status)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        if key in config:
            value = config.get(key)
            return default if value is None else value
        node: Any = config
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is None else node
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        if value is not None:
            return value
    return default


def _config_bool(config: Any, key: str, default: bool = False) -> bool:
    value = _config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _absolute_directory(value: str) -> str:
    """Validate the HTTP path shape without changing the submitted value."""

    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise CLIDevelopmentSessionRouteError(
            "invalid_working_directory",
            "working_directory must be an absolute path",
            422,
        )
    try:
        candidate = Path(value)
        if candidate.parent == candidate:
            raise CLIDevelopmentSessionRouteError(
                "invalid_working_directory",
                "working_directory must not be a filesystem root",
                422,
            )
    except (OSError, ValueError):
        raise CLIDevelopmentSessionRouteError(
            "invalid_working_directory",
            "working_directory is invalid",
            422,
        ) from None
    return value


def _owner_from_user(user: Any) -> dict[str, str]:
    if isinstance(user, Mapping):
        user_id = user.get("id") or user.get("user_id")
        role = user.get("role")
        display_name = user.get("display_name") or user.get("username") or ""
    else:
        user_id = getattr(user, "id", None) or getattr(user, "user_id", None)
        role = getattr(user, "role", None)
        display_name = getattr(user, "display_name", None) or getattr(user, "username", None) or ""
    try:
        owner_id = str(UUID(str(user_id)))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=403, detail="Authenticated human UUID is required") from None
    normalized_role = str(role or "admin").strip().lower()
    if normalized_role != "admin":
        raise HTTPException(status_code=403, detail="Administrator privileges required")
    return {
        "owner_id": owner_id,
        "owner_role": normalized_role,
        "display_name": str(display_name or "")[:255],
        "actor_type": "human",
    }


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


_SERVICE_METHODS = {
    "create": "create_session",
    "list": "list_sessions",
    "get": "get_session",
    "message": "send",
    "events": "list_events",
    "cancel": "cancel",
    "reconnect": "reconnect",
}


def _service_method(service: Any, operation: str) -> Callable[..., Any] | None:
    """Resolve the canonical CLIDevelopmentSessionService method."""

    name = _SERVICE_METHODS.get(operation)
    method = getattr(service, name, None) if name else None
    return method if callable(method) else None


async def _invoke(service: Any, operation: str, **kwargs: Any) -> Any:
    method = _service_method(service, operation)
    if method is None:
        raise CLIDevelopmentSessionRouteError(
            "service_unavailable",
            f"CLI development session service operation is unavailable: {operation}",
            503,
        )
    return await _maybe(method(**kwargs))


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"result": value}


def _session_projection(value: Any) -> dict[str, Any]:
    """Expose latest Run/Work IDs beside their bounded nested projections."""

    payload = _as_mapping(value)
    nested = payload.get("session")
    if isinstance(nested, Mapping):
        for key, item in nested.items():
            payload.setdefault(key, item)
    run = payload.get("run")
    work = payload.get("work")
    if payload.get("run_id") in (None, "") and isinstance(run, Mapping):
        payload["run_id"] = run.get("id")
    if payload.get("work_item_id") in (None, "") and isinstance(work, Mapping):
        payload["work_item_id"] = work.get("id")
    return payload


_COLLECT_HEADER_FIELDS = (
    "session_id",
    "owner_id",
    "agent_id",
    "agent_revision_id",
    "binding_id",
    "provider",
    "model",
    "effort",
    "working_directory",
    "role_context_mode",
    "role_context_sha256",
    "execution_target",
    "local_filesystem",
    "sandbox",
    "turn_status",
    "delivery",
    "status",
    "native_state",
    "run_id",
    "work_item_id",
)


def _collect_header(value: Any, *, session_id: str) -> dict[str, Any]:
    projection = _session_projection(value)
    projection.setdefault("session_id", session_id)
    # Legacy/fake service projections may omit role context or explicitly
    # return null.  The public summary contract treats that as raw mode.
    if projection.get("role_context_mode") in (None, ""):
        projection["role_context_mode"] = "raw"
    return {key: projection.get(key) for key in _COLLECT_HEADER_FIELDS}


def _collect_error(exc: BaseException) -> dict[str, str]:
    code = str(getattr(exc, "code", "") or "service_error")[:96]
    detail = str(getattr(exc, "message", "") or exc or "Request failed")[:500]
    return {"code": code, "detail": detail}


def _error_response(exc: BaseException) -> JSONResponse:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=int(exc.status_code),
            content={"detail": detail, "code": "http_error"},
        )
    code = str(getattr(exc, "code", "") or "service_error")[:96]
    message = str(getattr(exc, "message", "") or exc or "Request failed")[:500]
    raw_status = getattr(exc, "http_status", None)
    if raw_status is None:
        raw_status = getattr(exc, "status_code", None)
    try:
        status = int(raw_status)
    except (TypeError, ValueError):
        status = 409
    if status < 400 or status > 599:
        status = 409
    return JSONResponse(status_code=status, content={"detail": message, "code": code})


def create_cli_development_session_router(
    *,
    require_admin: Callable[..., Any],
    get_user_from_request: Callable[[Request], Any],
    get_service: Callable[[], Any] | None = None,
    service: Any | None = None,
    get_config: Callable[[], Any] | None = None,
) -> APIRouter:
    """Build the authenticated CLI development session router."""

    router = APIRouter(
        prefix="/api/cli-sessions",
        tags=["cli-development-sessions"],
        dependencies=[Depends(require_admin)],
    )

    def _config() -> Any:
        return get_config() if callable(get_config) else None

    def _resolve_service() -> Any | None:
        if service is not None:
            return service
        if callable(get_service):
            return get_service()
        return None

    def _require_service() -> Any:
        candidate = _resolve_service()
        if candidate is None:
            raise CLIDevelopmentSessionRouteError(
                "cli_development_sessions_disabled",
                "CLI development sessions are disabled or unavailable",
                409,
            )
        if not _config_bool(_config(), "cli_development_sessions.enabled", False):
            raise CLIDevelopmentSessionRouteError(
                "cli_development_sessions_disabled",
                "CLI development sessions are disabled",
                409,
            )
        return candidate

    async def _owner(request: Request) -> dict[str, str]:
        value = get_user_from_request(request)
        return _owner_from_user(await _maybe(value))

    async def _call(operation: str, http_request: Request, **kwargs: Any) -> Any:
        owner = await _owner(http_request)
        payload = {
            **kwargs,
            "owner_id": owner["owner_id"],
        }
        if operation == "create":
            payload["actor_role"] = owner["owner_role"]
        try:
            return await _invoke(_require_service(), operation, **payload)
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            raise exc
        except Exception as exc:
            # Service implementations expose typed ``code/http_status``
            # errors.  Unknown exceptions stay generic and never leak paths,
            # provider payloads or raw input into an API response.
            if getattr(exc, "code", None) is not None or getattr(exc, "http_status", None) is not None:
                raise exc
            raise CLIDevelopmentSessionRouteError("service_error", "CLI development session request failed", 503) from exc

    @router.get("/status", response_model=CLIDevelopmentSessionStatusResponse)
    async def cli_development_session_status() -> dict[str, Any]:
        candidate = _resolve_service()
        config = _config()
        configured = _config_bool(config, "cli_development_sessions.enabled", False)
        default_permission_mode = cli_development_policy(config).default_permission_mode()
        payload: dict[str, Any] = {
            "success": True,
            "enabled": configured,
            "available": candidate is not None,
            "trusted_windows": {
                "enabled": _config_bool(_config(), "cli_development_sessions.trusted_windows.enabled", False),
                "allowed_roots": _config_get(
                    _config(),
                    "cli_development_sessions.trusted_windows.allowed_roots",
                    [],
                ),
                "default_permission_mode": default_permission_mode,
            },
        }
        if candidate is not None:
            method = getattr(candidate, "policy_diagnostic", None)
            if callable(method):
                try:
                    described = await _maybe(method())
                    payload.update(_as_mapping(described))
                    effective = _as_mapping(payload.get("effective"))
                    trusted_windows = _as_mapping(payload.get("trusted_windows"))
                    if "default_permission_mode" in effective:
                        payload["trusted_windows"] = {
                            **trusted_windows,
                            "default_permission_mode": effective["default_permission_mode"],
                        }
                except Exception:
                    # Diagnostics must remain safe and bounded even when the
                    # optional runtime is not ready.
                    payload["available"] = False
        return payload

    @router.post("", status_code=202, response_model=CLIDevelopmentSessionResponse)
    async def create_cli_development_session(
        body: CLIDevelopmentSessionCreateRequest,
        request: Request,
    ) -> JSONResponse:
        try:
            payload = body.model_dump(mode="json", exclude_none=True)
            payload["_provided_fields"] = sorted(body.model_fields_set)
            if body.working_directory is not None:
                payload["working_directory"] = _absolute_directory(body.working_directory)
            result = await _call("create", request, request=payload)
            response = {"success": True, "accepted": True, **_session_projection(result)}
            return JSONResponse(status_code=202, content=response)
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.get("", response_model=CLIDevelopmentSessionListResponse)
    async def list_cli_development_sessions(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        status: CLISessionStatus | None = Query(default=None),
    ) -> Any:
        try:
            result = await _call("list", request, limit=limit, status=status)
            payload = _as_mapping(result)
            payload.setdefault("success", True)
            if "sessions" not in payload and isinstance(result, list):
                payload["sessions"] = result
            if isinstance(payload.get("sessions"), list):
                payload["sessions"] = [_session_projection(item) for item in payload["sessions"]]
            return payload
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.post("/collect", response_model=CLIDevelopmentSessionCollectResponse)
    async def collect_cli_development_session_events(
        body: CLIDevelopmentSessionCollectRequest,
        request: Request,
    ) -> Any:
        try:
            owner = await _owner(request)
            service = _require_service()
            if body.session_ids is None:
                discovered = await _invoke(
                    service,
                    "list",
                    owner_id=owner["owner_id"],
                    limit=200,
                )
                discovered_payload = _as_mapping(discovered)
                raw_sessions = (
                    discovered_payload.get("sessions")
                    if "sessions" in discovered_payload
                    else discovered
                )
                session_ids = [
                    str(item.get("session_id") or "")
                    for item in (raw_sessions if isinstance(raw_sessions, list) else [])
                    if isinstance(item, Mapping) and item.get("session_id")
                ]
                # A cursor may refer to a session not returned by the bounded
                # list page; include it so the caller can still recover that
                # page and receive an item-level authorization/not-found error.
                session_ids.extend(
                    key for key in body.cursors.keys() if key not in session_ids
                )
            else:
                # An explicit empty list means "collect nothing" even when a
                # cursor map was supplied.
                session_ids = list(body.session_ids)
            session_ids = list(dict.fromkeys(session_ids))
            sessions: list[dict[str, Any]] = []
            for session_id in session_ids:
                try:
                    header_source = await _invoke(
                        service,
                        "get",
                        session_id=session_id,
                        owner_id=owner["owner_id"],
                    )
                    item = _collect_header(header_source, session_id=session_id)
                except Exception as exc:
                    sessions.append({
                        "session_id": session_id,
                        "error": _collect_error(exc),
                        "events": [],
                        "next_cursor": body.cursors.get(session_id),
                        "has_more": False,
                    })
                    continue
                try:
                    page = await _invoke(
                        service,
                        "events",
                        session_id=session_id,
                        owner_id=owner["owner_id"],
                        after_cursor=body.cursors.get(session_id),
                        limit=body.limit_per_session,
                    )
                    page_payload = _as_mapping(page)
                    item.update({
                        "events": page_payload.get("events") or [],
                        "next_cursor": page_payload.get("next_cursor"),
                        "has_more": bool(page_payload.get("has_more", False)),
                    })
                except Exception as exc:
                    item.update({
                        "events": [],
                        "next_cursor": body.cursors.get(session_id),
                        "has_more": False,
                        "error": _collect_error(exc),
                    })
                sessions.append(item)
            return {"success": True, "sessions": sessions}
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.get("/{session_id}", response_model=CLIDevelopmentSessionResponse)
    async def get_cli_development_session(session_id: str, request: Request) -> Any:
        try:
            result = await _call("get", request, session_id=session_id)
            payload = _session_projection(result)
            payload.setdefault("success", True)
            return payload
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.post("/{session_id}/messages", status_code=202, response_model=CLIDevelopmentSessionResponse)
    async def send_cli_development_session_message(
        session_id: str,
        body: CLIDevelopmentSessionMessageRequest,
        request: Request,
    ) -> JSONResponse:
        try:
            # Deliberately pass body.message verbatim.  Do not call strip(),
            # normalize whitespace, or rebuild the request from tokens.
            result = await _call(
                "message",
                request,
                session_id=session_id,
                request={
                    "content": body.message,
                    "send_key": body.idempotency_key,
                },
            )
            return JSONResponse(status_code=202, content={"success": True, "accepted": True, **_session_projection(result)})
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.get("/{session_id}/events", response_model=CLIDevelopmentSessionEventsResponse)
    async def get_cli_development_session_events(
        session_id: str,
        request: Request,
        after: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Any:
        try:
            result = await _call(
                "events",
                request,
                session_id=session_id,
                after_cursor=after,
                limit=limit,
            )
            payload = _as_mapping(result)
            payload.setdefault("success", True)
            payload.setdefault("session_id", session_id)
            return payload
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.post("/{session_id}/cancel", status_code=202, response_model=CLIDevelopmentSessionActionResponse)
    async def cancel_cli_development_session(session_id: str, request: Request) -> JSONResponse:
        try:
            current = await _call("get", request, session_id=session_id)
            current_mapping = _as_mapping(current)
            run = current_mapping.get("run")
            run_id = current_mapping.get("run_id")
            if run_id in (None, "") and isinstance(run, Mapping):
                run_id = run.get("id")
            if not run_id:
                raise CLIDevelopmentSessionRouteError(
                    "run_not_available",
                    "No active CLI session run is available for cancellation",
                    409,
                )
            result = await _call(
                "cancel",
                request,
                session_id=session_id,
                run_id=str(run_id),
            )
            return JSONResponse(status_code=202, content={"success": True, "accepted": True, **_as_mapping(result)})
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    @router.post("/{session_id}/reconnect", status_code=202, response_model=CLIDevelopmentSessionActionResponse)
    async def reconnect_cli_development_session(session_id: str, request: Request) -> JSONResponse:
        try:
            result = await _call("reconnect", request, session_id=session_id)
            return JSONResponse(status_code=202, content={"success": True, "accepted": True, **_as_mapping(result)})
        except (HTTPException, CLIDevelopmentSessionRouteError) as exc:
            return _error_response(exc)
        except Exception as exc:
            return _error_response(exc)

    return router


__all__ = [
    "CLIProvider",
    "CLISessionStatus",
    "CLIDevelopmentSessionActionResponse",
    "CLIDevelopmentSessionCollectRequest",
    "CLIDevelopmentSessionCollectError",
    "CLIDevelopmentSessionCollectItem",
    "CLIDevelopmentSessionCollectResponse",
    "CLIDevelopmentSessionCreateRequest",
    "CLIDevelopmentSessionEvent",
    "CLIDevelopmentSessionEventPage",
    "CLIDevelopmentSessionEventsResponse",
    "CLIDevelopmentSessionListResponse",
    "CLIDevelopmentSessionMessageRequest",
    "CLIDevelopmentSessionOutputChunk",
    "CLIDevelopmentSessionResponse",
    "CLIDevelopmentSessionRouteError",
    "CLIDevelopmentSessionSummary",
    "CLIDevelopmentSessionPolicyContext",
    "CLIDevelopmentSessionPolicyEffective",
    "CLIDevelopmentSessionStatusResponse",
    "CLIDevelopmentSessionTrustedWindowsStatus",
    "create_cli_development_session_router",
]
