"""Durable sessions for trusted, Windows-installed development CLIs.

The HTTP/API layer is intentionally kept outside this module.  This service
owns admission and the durable Conversation/Run/Work records; execution is
performed by :mod:`cli_session_work_adapter` through the common Work
coordinator.  It does not start a provider process or create a private queue.

The trusted Windows lane is an explicit deployment policy.  It is disabled by
default because it is a Windows-user authority boundary, rather than an OS
sandbox.  Every request still rechecks the policy, the immutable Agent
binding, and the effective Agent capabilities before it is admitted.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    AgentExecutionBinding,
    AgentRun,
    AgentRunEvent,
    AgentWorkEvent,
    AgentWorkItem,
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
)
from .agent_authority import AgentAuthorityResolver
from .agent_role_context import AgentRoleContext, build_agent_role_context
from .conversation_agent_service import (
    AgentExecutionBindingSnapshot,
    ConversationAgentConflictError,
    ConversationAgentService,
    ConversationAgentValidationError,
)
from .muse_remote_execution import (
    MUSE_ADAPTER_KEY as MUSE_REMOTE_ADAPTER_KEY,
    MUSE_EXECUTION_TARGET,
    MUSE_PROVIDER,
    REMOTE_AGENT_DELEGATE,
)
from .muse_remote_session_store import (
    MUSE_REMOTE_CONTEXT_KEY,
    MuseRemotePrivacyGateway,
    MuseRemoteSessionStore,
    MuseRemoteTargetResolver,
)


CLI_SESSION_SOURCE_TYPE = "cli_session_turn"
CLI_SESSION_ADAPTER_KEY = "cli_session_turn"
REMOTE_SESSION_SOURCE_TYPE = MUSE_REMOTE_ADAPTER_KEY
CLI_DEVELOPMENT_DOMAIN = "cli_development"
CLI_DEVELOPMENT_CONTEXT_KEY = "cli_development"
CLI_DEVELOPMENT_SCHEMA_VERSION = 1
CLI_DEVELOPMENT_CAPABILITIES = (
    "command_execute",
    "workspace_read",
    "workspace_write",
)
CLI_DEVELOPMENT_STATUSES = frozenset(
    {
        "ready",
        "queued",
        "running",
        "completed",
        "failed",
        "waiting_input",
        "waiting_approval",
        "disconnected",
        "interrupted",
        "cancelled",
        "cancelling",
    }
)
CLI_DEVELOPMENT_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "disconnected", "interrupted"}
)
CLI_DEVELOPMENT_PERMISSION_MODES = frozenset({"native_default", "standard", "accept_edits"})
CLI_ROLE_CONTEXT_MODES = frozenset({"raw", "agent_role"})
CLI_ROLE_CONTEXT_MAX_BYTES = 128 * 1024
CLI_PROVIDER_PERMISSION_MODES = {
    "antigravity-cli": frozenset({"native_default", "standard", "accept_edits"}),
    "claude-cli": frozenset({"native_default", "standard", "accept_edits"}),
    "codex-cli": frozenset({"native_default", "standard"}),
    "grok-cli": frozenset({"native_default", "standard"}),
}
REMOTE_SESSION_CAPABILITIES = (REMOTE_AGENT_DELEGATE,)


class CLIDevelopmentSessionError(RuntimeError):
    """A stable, API-safe error raised by this service."""

    status_code = 400
    code = "cli_development_session_error"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = str(code or self.code)
        super().__init__(message or self.code)


class CLIDevelopmentSessionNotFound(CLIDevelopmentSessionError):
    status_code = 404
    code = "cli_development_session_not_found"


class CLIDevelopmentSessionForbidden(CLIDevelopmentSessionError):
    status_code = 403
    code = "cli_development_session_forbidden"


class CLIDevelopmentSessionConflict(CLIDevelopmentSessionError):
    status_code = 409
    code = "cli_development_session_conflict"


class CLIDevelopmentSessionInvalid(CLIDevelopmentSessionError):
    status_code = 400
    code = "cli_development_session_invalid"


class CLIDevelopmentSessionUnavailable(CLIDevelopmentSessionError):
    status_code = 409
    code = "cli_development_session_unavailable"


class CLIDevelopmentSessionStorageError(CLIDevelopmentSessionError):
    status_code = 503
    code = "cli_development_session_storage_error"


def _is_unique_integrity_error(exc: BaseException) -> bool:
    original = getattr(exc, "orig", None)
    state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if str(state or "") == "23505":
        return True
    detail = str(original or exc).casefold()
    return "unique constraint failed" in detail or "duplicate key value" in detail


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read a bounded config value without invoking arbitrary object methods."""

    if isinstance(config, Mapping):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                return default
    return getattr(config, key, default)


def _section(config: Any) -> Mapping[str, Any]:
    raw = _config_get(config, "cli_development_sessions", {})
    return _as_mapping(raw)


def _text(value: Any, *, field_name: str, limit: int = 512, required: bool = True) -> str:
    if value is None:
        text = ""
    else:
        text = str(value)
    if required and not text:
        raise CLIDevelopmentSessionInvalid(
            f"{field_name} is required", code=f"{field_name}_required"
        )
    if len(text) > limit:
        raise CLIDevelopmentSessionInvalid(
            f"{field_name} is too long", code=f"{field_name}_too_long"
        )
    return text


def _identifier(value: Any, *, field_name: str, limit: int = 255) -> str:
    # Identifiers may be normalized for matching.  Prompt/content values never
    # pass through this helper, so their leading/trailing whitespace survives.
    text = _text(value, field_name=field_name, limit=limit)
    normalized = text.strip()
    if not normalized:
        raise CLIDevelopmentSessionInvalid(
            f"{field_name} is required", code=f"{field_name}_required"
        )
    return normalized


def normalize_cli_capabilities(value: Any) -> tuple[str, ...]:
    """Return the canonical capability ceiling.

    ``None`` means the legacy full ceiling.  An explicit empty or unknown
    value is rejected so it can never widen back to the default.
    """

    if value is None:
        return CLI_DEVELOPMENT_CAPABILITIES
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise CLIDevelopmentSessionInvalid("allowed_capabilities must be a list", code="allowed_capabilities_invalid")
    items = [item.strip() if isinstance(item, str) else None for item in value]
    if not items or any(not item or item not in CLI_DEVELOPMENT_CAPABILITIES for item in items):
        raise CLIDevelopmentSessionInvalid("allowed_capabilities must be a non-empty subset of the known capabilities", code="allowed_capabilities_invalid")
    return tuple(sorted(set(items)))


def pinned_cli_capabilities(context: Mapping[str, Any]) -> tuple[str, ...]:
    """Read the ceiling pinned at create time; malformed state fails closed."""

    # Only a genuinely absent field is a legacy session; a present null or
    # malformed value is corrupted state and must not widen to the default.
    if "allowed_capabilities" not in context:
        return CLI_DEVELOPMENT_CAPABILITIES
    try:
        if context["allowed_capabilities"] is None:
            raise CLIDevelopmentSessionInvalid("allowed_capabilities is null", code="allowed_capabilities_invalid")
        return normalize_cli_capabilities(context["allowed_capabilities"])
    except CLIDevelopmentSessionInvalid as exc:
        raise CLIDevelopmentSessionUnavailable("pinned session capabilities are invalid", code="session_capabilities_invalid") from exc


def pinned_cli_role_context(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return metadata for an explicitly managed Agent role context.

    Raw and legacy sessions have no role-context fields.  The role text is
    deliberately rebuilt from the immutable binding at dispatch time and is
    never persisted in session, manifest, Work, Run, or event projections.
    """

    mode = context.get("role_context_mode")
    if mode in (None, ""):
        return None
    if mode != "agent_role":
        raise CLIDevelopmentSessionUnavailable(
            "pinned role context mode is invalid", code="role_context_invalid"
        )
    try:
        schema_version = int(context.get("role_context_schema_version"))
    except (TypeError, ValueError):
        schema_version = 0
    pins = {
        "mode": "agent_role",
        "context_schema_version": schema_version,
        "agent_id": str(context.get("role_context_agent_id") or ""),
        "agent_revision_id": str(context.get("role_context_agent_revision_id") or ""),
        "role_context_sha256": str(context.get("role_context_sha256") or ""),
    }
    if (
        schema_version != 1
        or not pins["agent_id"]
        or not pins["agent_revision_id"]
        or len(pins["role_context_sha256"]) != 64
    ):
        raise CLIDevelopmentSessionUnavailable(
            "pinned role context is incomplete", code="role_context_invalid"
        )
    return pins


def _build_pinned_role_context(
    binding: Any, context: Mapping[str, Any] | None = None
) -> AgentRoleContext:
    """Build role text and, when supplied, verify every persisted pin."""

    try:
        role_context = build_agent_role_context(binding)
    except (TypeError, ValueError, AttributeError) as exc:
        raise CLIDevelopmentSessionConflict(
            "Agent role context is unavailable", code="agent_role_context_unavailable"
        ) from exc
    if context is not None:
        pins = pinned_cli_role_context(context)
        expected = {
            "mode": role_context.mode,
            "context_schema_version": role_context.context_schema_version,
            "agent_id": role_context.agent_id,
            "agent_revision_id": role_context.agent_revision_id,
            "role_context_sha256": role_context.role_context_sha256,
        }
        if pins != expected:
            raise CLIDevelopmentSessionConflict(
                "Agent role context differs from the fixed session context",
                code="agent_role_context_mismatch",
            )
    return role_context


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        else:
            normalized = normalized.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    return str(value)


def _safe_uuid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _safe_json(value: Any, *, depth: int = 0, limit: int = 8) -> Any:
    """Return a small metadata-only JSON projection.

    This is deliberately conservative because AgentWork metadata is an
    operator projection, not a transcript store.  In particular, arbitrary
    text and filesystem paths are never copied into Work metadata.
    """

    if depth > limit:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:160]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            if any(marker in name.casefold() for marker in ("prompt", "content", "secret", "token", "password", "credential", "raw", "output")):
                continue
            projected = _safe_json(item, depth=depth + 1, limit=limit)
            if projected is not None:
                result[name[:96]] = projected
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, depth=depth + 1, limit=limit) for item in list(value)[:64]]
    return str(value)[:160]


def _bounded_text_chunk(content: str, start: int, end: int, *, max_chars: int = 4096, max_bytes: int = 16_384) -> tuple[str, int, bool]:
    """Slice decrypted assistant text without splitting a UTF-8 character."""

    source = str(content or "")
    begin = max(0, min(int(start or 0), len(source)))
    requested_end = max(begin, min(int(end if end is not None else begin + max_chars), len(source)))
    target = source[begin:min(requested_end, begin + max_chars)]
    encoded = bytearray()
    chars: list[str] = []
    for char in target:
        raw = char.encode("utf-8")
        if len(encoded) + len(raw) > max_bytes:
            break
        encoded.extend(raw)
        chars.append(char)
    text = "".join(chars)
    actual_end = begin + len(text)
    return text, actual_end, actual_end < requested_end


@dataclass(frozen=True)
class CLIDevelopmentPolicy:
    """Effective trusted-Windows policy with a safe diagnostic projection."""

    enabled: bool = False
    execution_lane: str = "disabled"
    allowed_roots: tuple[str, ...] = ()
    allowed_owner_ids: tuple[str, ...] = ()
    allowed_agent_ids: tuple[str, ...] = ()
    allowed_permission_modes: tuple[str, ...] = tuple(sorted(CLI_DEVELOPMENT_PERMISSION_MODES))
    provider_cache_roots: Mapping[str, str] = field(default_factory=dict)
    source: str = "default"
    platform: str = "windows"

    def default_permission_mode(self, provider: str | None = None) -> str | None:
        """Pick a non-elevating default supported by policy and providers.

        ``accept_edits`` remains available only as an explicit request.  The
        status projection has no provider selection, so it uses the modes
        supported by every registered CLI provider.
        """

        if provider is None:
            supported = set(CLI_DEVELOPMENT_PERMISSION_MODES)
            for provider_modes in CLI_PROVIDER_PERMISSION_MODES.values():
                supported.intersection_update(provider_modes)
        else:
            supported = set(
                CLI_PROVIDER_PERMISSION_MODES.get(
                    str(provider).strip().lower(), frozenset()
                )
            )
        allowed = set(self.allowed_permission_modes).intersection(supported)
        return next(
            (mode for mode in ("native_default", "standard") if mode in allowed),
            None,
        )

    def to_diagnostic(self, *, cwd: str | None = None, owner_id: str | None = None) -> dict[str, Any]:
        effective = {
            "enabled": bool(self.enabled),
            "execution_lane": self.execution_lane,
            "allowed_roots": list(self.allowed_roots),
            "allowed_owner_ids": list(self.allowed_owner_ids),
            "allowed_agent_ids": list(self.allowed_agent_ids),
            "default_permission_mode": self.default_permission_mode(),
            "allowed_permission_modes": list(self.allowed_permission_modes),
            "provider_cache_roots": dict(self.provider_cache_roots),
        }
        return {
            "source": self.source,
            "effective": effective,
            "policy": {
                "platform": self.platform,
                "cwd": cwd,
                "owner_id": owner_id,
                "windows_user_authority": self.execution_lane == "trusted_windows",
                "sandbox": False,
            },
        }


def cli_development_policy(config: Any | None = None) -> CLIDevelopmentPolicy:
    """Resolve policy without enabling the lane implicitly."""

    section = _section(config)
    trusted = _as_mapping(section.get("trusted_windows", {}))
    raw = trusted
    configured_enabled = section.get("enabled") is True and trusted.get("enabled") is True
    source = "config" if section else "default"
    roots: list[str] = []
    for item in raw.get("allowed_roots", ()) or ():
        if item not in (None, ""):
            roots.append(str(item))
    owners = tuple(dict.fromkeys(str(item).strip() for item in (raw.get("allowed_owner_ids", ()) or ()) if str(item).strip()))
    agents = tuple(dict.fromkeys(str(item).strip() for item in (raw.get("allowed_agent_ids", ()) or ()) if str(item).strip()))
    raw_cache_roots = raw.get("provider_cache_roots", {})
    cache_roots = {
        str(provider).strip().lower(): str(path)
        for provider, path in raw_cache_roots.items()
        if str(provider).strip() and path not in (None, "")
    } if isinstance(raw_cache_roots, Mapping) else {}
    lane = str(raw.get("execution_lane") or "disabled").strip().lower()
    if "allowed_permission_modes" not in raw:
        allowed_modes = ("native_default",)
    else:
        raw_allowed_modes = raw.get("allowed_permission_modes")
        if not isinstance(raw_allowed_modes, (list, tuple, set, frozenset)):
            allowed_modes = ()
        else:
            normalized_modes = tuple(
                str(item).strip().lower() if isinstance(item, str) else ""
                for item in raw_allowed_modes
            )
            allowed_modes = (
                tuple(dict.fromkeys(normalized_modes))
                if normalized_modes
                and all(item in CLI_DEVELOPMENT_PERMISSION_MODES for item in normalized_modes)
                else ()
            )
    platform_name = str(raw.get("platform") or "windows").strip().lower()
    return CLIDevelopmentPolicy(
        enabled=configured_enabled if section else False,
        execution_lane=lane,
        allowed_roots=tuple(roots),
        allowed_owner_ids=owners,
        allowed_agent_ids=agents,
        allowed_permission_modes=allowed_modes,
        provider_cache_roots=cache_roots,
        source=source,
        platform=platform_name,
    )


def _has_reparse_attribute(path: Path) -> bool:
    try:
        info = path.lstat()
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except (OSError, ValueError):
        return True


def canonical_windows_cwd(value: Any, roots: Sequence[str]) -> str:
    """Canonicalize a cwd and reject reparse paths outside trusted roots."""

    raw = _text(value, field_name="working_directory", limit=1024)
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise CLIDevelopmentSessionInvalid("working_directory must be absolute", code="working_directory_absolute_required")
    try:
        raw_current = Path(candidate.anchor or candidate.root)
        for part in candidate.parts[1:] if candidate.anchor else candidate.parts:
            raw_current = raw_current / part
            if _has_reparse_attribute(raw_current):
                raise CLIDevelopmentSessionForbidden("reparse paths are not trusted", code="working_directory_reparse_denied")
        resolved = Path(os.path.realpath(str(candidate)))
    except (OSError, ValueError) as exc:
        raise CLIDevelopmentSessionInvalid("working_directory cannot be resolved", code="working_directory_invalid") from exc
    if not resolved.exists() or not resolved.is_dir():
        raise CLIDevelopmentSessionInvalid("working_directory is unavailable", code="working_directory_unavailable")
    # Check each existing component.  A symlink/reparse component may point
    # back into an allowed root today and still be swapped before launch.
    current = Path(resolved.anchor or resolved.root)
    for part in resolved.parts[1:] if resolved.anchor else resolved.parts:
        current = current / part
        if _has_reparse_attribute(current):
            raise CLIDevelopmentSessionForbidden("reparse paths are not trusted", code="working_directory_reparse_denied")
    canonical_roots: list[Path] = []
    for raw_root in roots:
        raw_root_path = Path(str(raw_root))
        if not raw_root_path.is_absolute() or _has_reparse_attribute(raw_root_path):
            continue
        root = Path(os.path.realpath(str(raw_root_path)))
        if not root.exists() or not root.is_dir() or _has_reparse_attribute(root):
            continue
        canonical_roots.append(root)
    if not canonical_roots:
        raise CLIDevelopmentSessionForbidden("trusted Windows roots are unavailable", code="trusted_windows_roots_unavailable")
    for root in canonical_roots:
        try:
            resolved.relative_to(root)
            return str(resolved)
        except ValueError:
            continue
    raise CLIDevelopmentSessionForbidden("working_directory is outside trusted roots", code="working_directory_outside_allowed_root")


_CLI_BACKEND_ALIASES = frozenset({"cli", "native", "command_line"})


def _executable_routes(binding: Any) -> list[Mapping[str, Any]]:
    raw = getattr(binding, "route_snapshot", None)
    if raw is None and isinstance(binding, Mapping):
        raw = binding.get("route_snapshot")
    if not isinstance(raw, Mapping):
        return []
    roles = raw.get("roles", raw)
    if not isinstance(roles, Mapping):
        return []
    routes: list[Mapping[str, Any]] = []
    for route in roles.values():
        if not isinstance(route, Mapping) or route.get("error"):
            continue
        provider = str(route.get("provider") or "").strip().lower()
        if provider == MUSE_PROVIDER:
            if str(route.get("backend") or "").strip().lower() != "remote":
                continue
            if route.get("model") not in (None, "") or route.get("effort") not in (None, ""):
                continue
        elif not provider or not str(route.get("model") or "").strip():
            continue
        routes.append(route)
    return routes


def _binding_value(binding: Any, name: str, default: Any = None) -> Any:
    if isinstance(binding, Mapping):
        return binding.get(name, default)
    return getattr(binding, name, default)


def _assert_route(binding: Any, *, provider: str, model: str | None, effort: str | None) -> dict[str, Any]:
    routes = _executable_routes(binding)
    if not routes:
        raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable")
    want = (provider.strip().lower(), str(model or "").strip(), str(effort or "").strip())
    matches: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    declared_backends: dict[tuple[str, str, str, str], str] = {}
    for route in routes:
        route_provider = str(route.get("provider") or "").strip().lower()
        actual = (
            route_provider,
            str(route.get("model") or "").strip(),
            str(route.get("effort") or route.get("reasoning_effort") or "").strip(),
        )
        if actual != want:
            continue
        backend = str(route.get("backend") or "").strip().lower()
        kind = "remote" if route_provider == MUSE_PROVIDER else "cli" if not backend or backend in _CLI_BACKEND_ALIASES else backend
        if route_provider == MUSE_PROVIDER and backend != "remote":
            continue
        declared_backends.setdefault((*actual, kind), backend)
        matches.setdefault(
            (*actual, kind),
            {
                "provider": actual[0],
                "model": None if route_provider == MUSE_PROVIDER else actual[1],
                "effort": None if route_provider == MUSE_PROVIDER else actual[2],
                "backend": "remote" if route_provider == MUSE_PROVIDER else backend or "cli",
                "route_source": str(route.get("route_source") or "")[:80],
            },
        )
    if not matches:
        raise CLIDevelopmentSessionConflict(
            "Agent route does not match the fixed CLI session route",
            code="agent_route_mismatch",
        )
    if len(matches) > 1:
        raise CLIDevelopmentSessionConflict("Agent route is ambiguous", code="agent_route_ambiguous")
    (key, result), = matches.items()
    if key[3] == "remote":
        return result
    if key[3] != "cli":
        raise CLIDevelopmentSessionConflict("Agent route is not a CLI route", code="agent_route_backend_mismatch")
    if _binding_selected_profile(binding) is not None:
        # Explicit ExecutionProfile: an empty backend is not implied CLI and
        # the provider must have a registered CLI session adapter.
        from .agent_model_routing import AgentModelRouteError, assert_registered_route_backend

        raw_backend = declared_backends.get(key, "")
        declared = "cli" if raw_backend in _CLI_BACKEND_ALIASES else raw_backend
        try:
            assert_registered_route_backend(key[0], declared, ingress="cli")
        except AgentModelRouteError as exc:
            code = "cli_route_not_supported" if exc.code == "agent_route_provider_unsupported" else exc.code
            raise CLIDevelopmentSessionConflict("Agent route has no registered CLI adapter", code=code) from None
    return result


def _execution_profile_kwargs(execution_profile_id: Any) -> dict[str, Any]:
    # Revision-default sessions keep the historical bind call shape.
    return {} if execution_profile_id is None else {"execution_profile_id": execution_profile_id}


def _binding_selected_profile(binding: Any) -> str | None:
    selection = getattr(binding, "execution_selection", None)
    if selection is None or not getattr(selection, "is_explicit", False):
        return None
    return str(selection.selected_execution_profile_id)


def _assert_binding_selection(binding: Any, execution_profile_id: Any) -> None:
    """The immutable binding must carry exactly the admitted selection."""

    try:
        selected = _binding_selected_profile(binding)
    except Exception:
        raise CLIDevelopmentSessionConflict("Agent execution selection is inconsistent", code="agent_binding_selection_invalid") from None
    if selected != (str(execution_profile_id) if execution_profile_id is not None else None):
        raise CLIDevelopmentSessionConflict("Agent execution selection is inconsistent", code="agent_binding_selection_invalid")


def _binding_selection_manifest_fields(binding: Any, execution_profile_id: Any) -> dict[str, Any]:
    """Selection block for a Run manifest; empty for revision defaults.

    An explicit selection replaces the manifest ``route_snapshot`` with the
    binding's full frozen roles: the selection's ``role_backends`` digest
    covers every role, so routes, selection and header must come from the
    same immutable binding (``execution_manifest_route_fields``).
    """

    _assert_binding_selection(binding, execution_profile_id)
    if execution_profile_id is None:
        return {}
    from .agent_model_routing import execution_manifest_route_fields

    fields = execution_manifest_route_fields(getattr(binding, "route_snapshot", None))
    if not fields.get("route_snapshot") or fields.get("execution_profile_id") != execution_profile_id:
        raise CLIDevelopmentSessionConflict("Agent execution selection is inconsistent", code="agent_binding_selection_invalid")
    return fields


def _decision_allowed(decision: Any) -> bool:
    if isinstance(decision, Mapping):
        return bool(decision.get("allowed", False))
    return bool(getattr(decision, "allowed", False))


@dataclass(frozen=True)
class CreateCLIDevelopmentSessionRequest:
    owner_id: str
    agent_id: str
    agent_revision_id: str | None
    provider: str
    model: str | None
    effort: str
    working_directory: str | None
    idempotency_key: str
    actor_role: str | None = None
    title: str = ""
    project_id: str | None = None
    permission_mode: str | None = None
    allowed_capabilities: tuple[str, ...] = CLI_DEVELOPMENT_CAPABILITIES
    execution_profile_id: str | None = None
    role_context_mode: str = "raw"
    execution_target: str | None = None
    provided_fields: frozenset[str] = frozenset()

    @classmethod
    def from_value(cls, value: Any = None, **kwargs: Any) -> "CreateCLIDevelopmentSessionRequest":
        source = dict(value) if isinstance(value, Mapping) else {}
        source.update(kwargs)
        provided_raw = source.get("_provided_fields")
        provided = (
            frozenset(str(item) for item in provided_raw)
            if isinstance(provided_raw, (list, tuple, set, frozenset))
            else frozenset(str(key) for key in source)
        )
        provider = _identifier(source.get("provider"), field_name="provider", limit=80).lower()
        remote = provider == MUSE_PROVIDER
        if remote and provided.intersection(
            {"model", "working_directory", "permission_mode", "allowed_capabilities", "effort"}
        ):
            raise CLIDevelopmentSessionInvalid(
                "muse-remote does not accept local execution fields",
                code="remote_request_field_forbidden",
            )
        execution_target = source.get("execution_target")
        if remote and execution_target not in (None, MUSE_EXECUTION_TARGET):
            raise CLIDevelopmentSessionInvalid(
                "execution_target does not match the server remote target",
                code="remote_execution_target_mismatch",
            )
        caps = REMOTE_SESSION_CAPABILITIES if remote else normalize_cli_capabilities(source.get("allowed_capabilities"))
        execution_profile_id = source.get("execution_profile_id")
        if execution_profile_id is not None and (not isinstance(execution_profile_id, str) or not execution_profile_id):
            raise CLIDevelopmentSessionInvalid("execution_profile_id is invalid", code="agent_execution_profile_invalid")
        raw_role_context_mode = source.get("role_context_mode")
        role_context_mode = (
            "raw"
            if raw_role_context_mode is None
            else str(raw_role_context_mode).strip().lower()
        )
        if role_context_mode not in CLI_ROLE_CONTEXT_MODES:
            raise CLIDevelopmentSessionInvalid(
                "role_context_mode must be raw or agent_role",
                code="role_context_mode_invalid",
            )
        requested_revision = source.get("agent_revision_id")
        agent_revision_id = (
            None if requested_revision is None else _safe_uuid(requested_revision)
        )
        if requested_revision is not None and agent_revision_id is None:
            raise CLIDevelopmentSessionInvalid(
                "agent_revision_id is invalid", code="agent_revision_id_invalid"
            )
        return cls(
            owner_id=_identifier(source.get("owner_id", source.get("user_id")), field_name="owner_id"),
            agent_id=_identifier(source.get("agent_id"), field_name="agent_id"),
            agent_revision_id=agent_revision_id,
            provider=provider,
            model=(None if remote else _identifier(source.get("model"), field_name="model", limit=160)),
            effort=str(source.get("effort") or "").strip(),
            working_directory=(None if remote else _text(source.get("working_directory"), field_name="working_directory", limit=1024)),
            idempotency_key=_text(source.get("idempotency_key"), field_name="idempotency_key", limit=512),
            actor_role=str(source.get("actor_role") or source.get("role") or "") or None,
            title=str(source.get("title") or "")[:200],
            project_id=str(source.get("project_id")) if source.get("project_id") not in (None, "") else None,
            permission_mode=(
                None
                if source.get("permission_mode") is None
                else str(source["permission_mode"]).strip().lower()
            ),
            allowed_capabilities=caps,
            execution_profile_id=execution_profile_id,
            role_context_mode=role_context_mode,
            execution_target=MUSE_EXECUTION_TARGET if remote else execution_target,
            provided_fields=provided,
        )


@dataclass(frozen=True)
class SendCLIDevelopmentSessionRequest:
    owner_id: str
    content: str
    send_key: str

    @classmethod
    def from_value(cls, value: Any = None, **kwargs: Any) -> "SendCLIDevelopmentSessionRequest":
        source = dict(value) if isinstance(value, Mapping) else {}
        source.update(kwargs)
        if "content" not in source and "raw" in source:
            source["content"] = source["raw"]
        return cls(
            owner_id=_identifier(source.get("owner_id", source.get("user_id")), field_name="owner_id"),
            content=_text(source.get("content"), field_name="content", limit=128_000),
            send_key=_text(source.get("send_key", source.get("idempotency_key")), field_name="send_key", limit=512),
        )


@dataclass(frozen=True)
class EventPage:
    events: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {"events": list(self.events), "next_cursor": self.next_cursor, "has_more": self.has_more}


class MuseRemoteTurnWorkSource:
    """Source contract for already-admitted remote Muse turns."""

    source_type = REMOTE_SESSION_SOURCE_TYPE

    def __init__(self, service: "CLIDevelopmentSessionService") -> None:
        self.service = service

    async def discover(self, session: Any, *, now: Any = None) -> Sequence[Any]:
        del session, now
        # API admission creates the Work row directly. Discovery must never
        # replay a user turn or synthesize another remote side thread.
        return ()

    async def refresh(self, session: Any, claim: Any) -> bool:
        db = session
        owns = db is None
        if db is None:
            db = await self.service._session()
        try:
            work = await db.get(AgentWorkItem, uuid.UUID(str(getattr(claim, "work_item_id", ""))))
            if work is None or str(work.source_type or "") != REMOTE_SESSION_SOURCE_TYPE or str(work.execution_adapter or "") != MUSE_REMOTE_ADAPTER_KEY:
                return False
            run = await db.get(AgentRun, work.active_agent_run_id) if work.active_agent_run_id else None
            conversation = await db.get(ConversationSession, work.conversation_session_id)
            metadata = work.metadata_json if isinstance(work.metadata_json, Mapping) else {}
            owner = str(metadata.get("owner_id") or "")
            claim_metadata = getattr(claim, "metadata", {})
            if not isinstance(claim_metadata, Mapping):
                claim_metadata = {}
            claim_owner = str(claim_metadata.get("owner_id") or "")
            if (
                conversation is None
                or not owner
                or not claim_owner
                or owner != claim_owner
                or str(conversation.user_id) != owner
                or str(work.assigned_agent_id) != str(getattr(claim, "assigned_agent_id", None) or "")
            ):
                return False
            if run is None:
                return False
            return (
                str(work.agent_revision_id) == str(getattr(claim, "agent_revision_id", None) or "")
                and str(run.session_id) == str(work.conversation_session_id)
                and str(run.agent_id) == str(work.assigned_agent_id)
                and str(run.agent_revision_id) == str(work.agent_revision_id)
                and str(run.run_type or "") in {REMOTE_SESSION_SOURCE_TYPE, "autonomous_work"}
                and owner == str((getattr(claim, "metadata", {}) or {}).get("owner_id") or "")
            )
        except (TypeError, ValueError, AttributeError):
            return False
        finally:
            if owns:
                await self.service._close(db)


class CLIDevelopmentSessionService:
    """Admission and projection facade for the CLI development lane."""

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        config: Any | None = None,
        coordinator: Any | None = None,
        session_store: Any | None = None,
        agent_service: Any | None = None,
        authority_resolver: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._db_manager = db_manager
        self.config = config
        self.coordinator = coordinator
        self.session_store = session_store
        self.agent_service = agent_service or ConversationAgentService(db_manager, config=config)
        self.authority_resolver = authority_resolver or AgentAuthorityResolver(db_manager, config=config)
        self._clock = clock or _utcnow
        self.remote_adapter: Any | None = None
        self.remote_binding_store: Any | None = None

    async def _session(self) -> Any:
        manager = self._db_manager
        if manager is None:
            from ..memory.database import get_database_manager

            manager = get_database_manager()
        value = manager.get_session()
        return await value if inspect.isawaitable(value) else value

    async def _close(self, session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            value = close()
            if inspect.isawaitable(value):
                await value

    @staticmethod
    def _rollback(session: Any) -> None:
        return None

    async def _rollback_async(self, session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            value = rollback()
            if inspect.isawaitable(value):
                await value

    def policy_diagnostic(self, *, owner_id: str | None = None, working_directory: str | None = None) -> dict[str, Any]:
        policy = cli_development_policy(self.config)
        return policy.to_diagnostic(cwd=working_directory, owner_id=owner_id)

    def _remote_target_config(self) -> Mapping[str, Any]:
        config = self.config
        if isinstance(config, Mapping):
            section = config.get("remote_targets", {})
            return section.get("muse", {}) if isinstance(section, Mapping) and isinstance(section.get("muse", {}), Mapping) else {}
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                section = getter("remote_targets", {})
            except Exception:
                section = {}
            return section.get("muse", {}) if isinstance(section, Mapping) and isinstance(section.get("muse", {}), Mapping) else {}
        return {}

    def _get_remote_adapter(self) -> Any:
        """Return the server-owned Muse adapter used by the shared Work lane."""

        if self.remote_adapter is not None:
            return self.remote_adapter
        from .muse_remote_execution import MuseRemoteClient, MuseRemoteExecutionAdapter

        repository_root = Path(__file__).resolve().parents[2]
        muse_venv = repository_root / ".local" / "tools" / "muse-cli-0.3.2" / "venv"
        if os.name == "nt":
            executable = muse_venv / "Scripts" / "muse-cli.exe"
        else:
            executable = muse_venv / "bin" / "muse-cli"

        self.remote_binding_store = self.remote_binding_store or MuseRemoteSessionStore(
            self._db_manager
        )
        target = self._remote_target_config()
        self.remote_adapter = MuseRemoteExecutionAdapter(
            client=MuseRemoteClient(executable=str(executable)),
            credential_resolver=MuseRemoteTargetResolver(self.config),
            binding_store=self.remote_binding_store,
            privacy_gateway=MuseRemotePrivacyGateway(self.config),
            wait_seconds=target.get("wait_seconds", 30.0),
            reconcile_timeout_seconds=target.get("reconcile_timeout_seconds", 120.0),
            poll_interval_seconds=target.get("poll_interval_seconds", 2.0),
        )
        return self.remote_adapter

    @staticmethod
    def _remote_allowlisted(value: str, raw: Any) -> bool:
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            values = list(raw)
        else:
            values = []
        return bool(values) and value in {str(item).strip() for item in values if str(item).strip()}

    def _assert_remote_admission(self, *, owner_id: str, agent_id: str) -> Mapping[str, Any]:
        target = self._remote_target_config()
        if not bool(target.get("enabled") is True or str(target.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"}):
            raise CLIDevelopmentSessionUnavailable("Muse remote target is disabled", code="remote_target_disabled")
        try:
            from ..features import Features

            if Features.is_enterprise() or not Features.is_enabled("remote_server_view"):
                raise CLIDevelopmentSessionForbidden("Muse remote target is unavailable for this feature profile", code="remote_target_feature_denied")
        except CLIDevelopmentSessionError:
            raise
        except Exception as exc:
            raise CLIDevelopmentSessionUnavailable("Muse remote feature gate is unavailable", code="remote_target_feature_unavailable") from exc
        if not self._remote_allowlisted(owner_id, target.get("allowed_owner_ids")):
            raise CLIDevelopmentSessionForbidden("owner is not allowed for the Muse remote target", code="remote_target_owner_denied")
        if not self._remote_allowlisted(agent_id, target.get("allowed_agent_ids")):
            raise CLIDevelopmentSessionForbidden("Agent is not allowed for the Muse remote target", code="remote_target_agent_denied")
        credential_ref = ""
        for key in ("owner_credential_refs", "credentials_by_owner", "credential_refs"):
            refs = target.get(key)
            if isinstance(refs, Mapping) and refs.get(owner_id) not in (None, ""):
                credential_ref = str(refs.get(owner_id)).strip()
                break
        if not credential_ref and target.get("credential_ref") not in (None, ""):
            credential_ref = str(target.get("credential_ref")).strip()
        if not credential_ref:
            raise CLIDevelopmentSessionUnavailable("Muse owner credential reference is unavailable", code="remote_credential_ref_missing")
        if not bool(target.get("egress_enabled") is True or str(target.get("egress_enabled") or "").strip().lower() in {"1", "true", "yes", "on"}):
            raise CLIDevelopmentSessionUnavailable("Muse remote egress is disabled", code="remote_egress_disabled")
        return target

    def _policy_for_request(self, *, owner_id: str, agent_id: str, working_directory: str) -> tuple[CLIDevelopmentPolicy, str]:
        policy = cli_development_policy(self.config)
        if os.name != "nt" or policy.platform != "windows":
            raise CLIDevelopmentSessionUnavailable("trusted Windows lane is unavailable", code="trusted_windows_unavailable")
        if not policy.enabled or policy.execution_lane != "trusted_windows":
            raise CLIDevelopmentSessionUnavailable("trusted Windows lane is disabled", code="trusted_windows_disabled")
        if not policy.allowed_owner_ids:
            raise CLIDevelopmentSessionForbidden("trusted Windows owner allow-list is required", code="trusted_windows_owner_policy_missing")
        if owner_id not in policy.allowed_owner_ids:
            raise CLIDevelopmentSessionForbidden("owner is not allowed for the trusted Windows lane", code="trusted_windows_owner_denied")
        if policy.allowed_agent_ids and agent_id not in policy.allowed_agent_ids:
            raise CLIDevelopmentSessionForbidden("Agent is not allowed for the trusted Windows lane", code="trusted_windows_agent_denied")
        cwd = canonical_windows_cwd(working_directory, policy.allowed_roots)
        return policy, cwd

    @staticmethod
    def _validate_permission_mode(policy: CLIDevelopmentPolicy, requested: str) -> str:
        mode = str(requested or "").strip().lower()
        if mode not in CLI_DEVELOPMENT_PERMISSION_MODES:
            raise CLIDevelopmentSessionInvalid("unsupported permission mode", code="permission_mode_invalid")
        if mode not in policy.allowed_permission_modes:
            raise CLIDevelopmentSessionForbidden("permission mode is outside the deployment allow-list", code="permission_mode_denied")
        return mode

    @staticmethod
    def _validate_provider_permission_mode(provider: str, mode: str) -> None:
        supported = CLI_PROVIDER_PERMISSION_MODES.get(str(provider or "").strip().lower(), frozenset())
        if mode not in supported:
            raise CLIDevelopmentSessionInvalid(
                "provider does not support the requested permission mode",
                code="permission_mode_unsupported",
            )

    @classmethod
    def _resolve_permission_mode(
        cls,
        policy: CLIDevelopmentPolicy,
        provider: str,
        requested: str | None,
    ) -> str:
        if requested is None:
            mode = policy.default_permission_mode(provider)
            if mode is None:
                raise CLIDevelopmentSessionUnavailable(
                    "no safe default permission mode is available for this provider",
                    code="permission_mode_default_unavailable",
                )
        else:
            mode = cls._validate_permission_mode(policy, requested)
        cls._validate_provider_permission_mode(provider, mode)
        return mode

    @staticmethod
    def _provider_cache_root(policy: CLIDevelopmentPolicy, provider: str) -> str:
        raw = policy.provider_cache_roots.get(str(provider or "").strip().lower()) or policy.provider_cache_roots.get("*")
        if not raw:
            raise CLIDevelopmentSessionUnavailable("provider cache root is not explicitly configured", code="provider_cache_root_missing")
        lexical = Path(str(raw))
        if not lexical.is_absolute():
            raise CLIDevelopmentSessionForbidden("provider cache root is not trusted", code="provider_cache_root_invalid")
        current = Path(lexical.anchor or lexical.root)
        for part in lexical.parts[1:] if lexical.anchor else lexical.parts:
            current = current / part
            if _has_reparse_attribute(current):
                raise CLIDevelopmentSessionForbidden("provider cache root contains a reparse path", code="provider_cache_root_reparse_denied")
        path = Path(os.path.realpath(str(lexical)))
        if not path.exists() or not path.is_dir() or _has_reparse_attribute(path):
            raise CLIDevelopmentSessionForbidden("provider cache root is not trusted", code="provider_cache_root_invalid")
        return str(path)

    async def _resolve_authority(
        self,
        *,
        agent_id: str,
        revision_id: str,
        project_id: str | None,
        capabilities: Iterable[str],
    ) -> dict[str, Any]:
        decisions: dict[str, Any] = {}
        for capability in tuple(dict.fromkeys(str(item).strip() for item in capabilities if str(item).strip())):
            try:
                decision = await self.authority_resolver.resolve(
                    agent_id=agent_id,
                    revision_id=revision_id,
                    project_id=project_id,
                    required_capability=capability,
                    tool_capabilities=tuple(capabilities),
                    harness_capabilities=tuple(capabilities),
                )
            except Exception as exc:
                raise CLIDevelopmentSessionUnavailable("Agent authority is unavailable", code="agent_authority_unavailable") from exc
            decisions[capability] = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision) if isinstance(decision, Mapping) else {"allowed": bool(getattr(decision, "allowed", False))}
            if not _decision_allowed(decision):
                reason = getattr(decision, "reason", None) or decisions[capability].get("reason") or "agent_authority_denied"
                raise CLIDevelopmentSessionForbidden("Agent authority denied", code=f"agent_authority_{str(reason)[:80]}")
        return decisions

    @staticmethod
    def _session_identity(owner_id: str, idempotency_key: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:cli-development-session:v1:{owner_id}:{idempotency_key}")

    @staticmethod
    def _wire_create_fingerprint(
        request: CreateCLIDevelopmentSessionRequest,
        *,
        permission_mode_override: str | None = None,
        role_context_sha256: str | None = None,
        resolved_agent_revision_id: str | None = None,
    ) -> str:
        """Fingerprint only caller supplied create fields.

        Deployment-derived roots/policies are deliberately excluded so a
        lost response can reconcile the same idempotent request after a
        config reload; they remain pinned in the session context separately.
        """

        material = {
            "owner_id": request.owner_id,
            "agent_id": request.agent_id,
            "provider": request.provider,
            "model": request.model,
            "effort": request.effort,
            "working_directory": request.working_directory,
            "project_id": request.project_id,
            "permission_mode": (
                request.permission_mode
                if permission_mode_override is None
                else permission_mode_override
            ),
            "title": request.title,
            "allowed_capabilities": list(request.allowed_capabilities),
        }
        if request.provider == MUSE_PROVIDER:
            material["execution_target"] = request.execution_target or MUSE_EXECUTION_TARGET
        if request.execution_profile_id is not None:
            # Only an explicit runtime selection joins the identity; omitted
            # keeps every historical fingerprint byte-identical.
            material["execution_profile_id"] = request.execution_profile_id
        if request.agent_revision_id is not None:
            # Exact-revision callers opt into a new identity component;
            # raw/legacy callers keep their historical request shape.
            material["agent_revision_id"] = request.agent_revision_id
        elif (
            request.role_context_mode == "agent_role"
            and resolved_agent_revision_id is not None
        ):
            # Keep caller omission distinct from an explicit wire pin while
            # binding managed admission to the revision resolved by authority.
            material["resolved_agent_revision_id"] = resolved_agent_revision_id
        if request.role_context_mode == "agent_role":
            material["role_context_mode"] = "agent_role"
            if role_context_sha256:
                material["role_context_sha256"] = role_context_sha256
        return _json_hash(material)

    @classmethod
    def _wire_create_fingerprint_matches(
        cls,
        request: CreateCLIDevelopmentSessionRequest,
        context: Mapping[str, Any],
        wire_fingerprint: str,
    ) -> bool:
        stored = context.get("wire_request_fingerprint")
        if stored == wire_fingerprint:
            return True
        # Before omitted mode was represented distinctly, an omitted value
        # was fingerprinted as native_default. Accept that receipt only when
        # the persisted mode confirms the same historical interpretation.
        if request.permission_mode is None and context.get("permission_mode") == "native_default":
            return stored == cls._wire_create_fingerprint(
                request, permission_mode_override="native_default"
            )
        return False

    @staticmethod
    def _context_tuple(
        *,
        owner_id: str,
        agent_id: str,
        provider: str,
        model: str | None,
        effort: str,
        working_directory: str | None,
        idempotency_key: str,
        project_id: str | None,
        permission_mode: str | None,
        title: str = "",
        allowed_capabilities: Sequence[str] = CLI_DEVELOPMENT_CAPABILITIES,
        execution_target: str | None = None,
    ) -> dict[str, Any]:
        context = {
            "schema_version": CLI_DEVELOPMENT_SCHEMA_VERSION,
            "owner_id": owner_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "effort": effort,
            "working_directory": working_directory,
            "idempotency_digest": _sha256(idempotency_key),
            "title": title,
            "allowed_capabilities": list(allowed_capabilities),
            "project_id": project_id,
            "permission_mode": permission_mode,
            "status": "queued",
            "admission_state": "creating",
            "native_state": "unknown",
            "event_sequence": 0,
        }
        if execution_target:
            context["execution_target"] = execution_target
        deployment_material = {
            "owner_id": owner_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "effort": effort,
            "working_directory": working_directory,
            "project_id": project_id,
            "permission_mode": permission_mode,
            "title": title,
            "allowed_capabilities": list(allowed_capabilities),
        }
        if execution_target:
            deployment_material["execution_target"] = execution_target
        context["deployment_fingerprint"] = _json_hash(deployment_material)
        return context

    @staticmethod
    def _context(row: Any) -> dict[str, Any]:
        context = getattr(row, "context", None)
        if not isinstance(context, Mapping):
            return {}
        raw = context.get(CLI_DEVELOPMENT_CONTEXT_KEY)
        return dict(raw) if isinstance(raw, Mapping) else {}

    @staticmethod
    def _is_remote_context(context: Mapping[str, Any]) -> bool:
        return str(context.get("provider") or "").strip().lower() == MUSE_PROVIDER

    @classmethod
    def _allocate_event_sequences(cls, row: Any, count: int) -> list[int]:
        """Allocate fixed session feed positions while the row is locked."""

        context = cls._context(row)
        current = max(0, int(context.get("event_sequence", 0) or 0))
        amount = max(0, int(count or 0))
        allocated = list(range(current + 1, current + amount + 1))
        if allocated:
            context["event_sequence"] = allocated[-1]
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
        return allocated

    @staticmethod
    def _status_from_rows(context: Mapping[str, Any], run: Any | None, work: Any | None) -> str:
        if str(context.get("admission_state") or "") == "creating":
            return "queued"
        if str(getattr(work, "state", "") or "").casefold() == "cancelled" and str(getattr(run, "status", "") or "").casefold() not in {"cancelled", "failed", "succeeded"}:
            return "cancelling"
        work_state = str(getattr(work, "state", "") or "").lower()
        run_state = str(getattr(run, "status", "") or "").lower()
        outcome = str(getattr(work, "outcome_classification", "") or "").lower()
        code = str(getattr(work, "safe_error_code", "") or "").lower()
        if work_state == "cancelled" or run_state == "cancelled":
            return "cancelled"
        if work_state == "awaiting_approval" or run_state == "awaiting_approval":
            return "waiting_approval"
        if outcome == "uncertain" or work_state == "uncertain":
            return "interrupted" if "interrupt" in code else "disconnected"
        if work_state == "blocked" and "input" in code:
            return "waiting_input"
        if work_state in {"running", "claimed"} or run_state == "running":
            return "running"
        if work_state in {"pending", "retry_wait"} or run_state == "queued":
            return "queued"
        if work_state == "succeeded" or run_state == "succeeded":
            return "completed"
        if work_state in {"failed", "dead_letter"} or run_state == "failed":
            return "failed"
        explicit = str(context.get("status") or "").strip().lower()
        if explicit in CLI_DEVELOPMENT_STATUSES and explicit not in {"ready", "queued", "running"}:
            return explicit
        return "ready"

    async def _load_latest(self, session: Any, session_id: uuid.UUID) -> tuple[Any | None, Any | None]:
        run = (await session.execute(select(AgentRun).where(AgentRun.session_id == session_id, AgentRun.run_type.in_((CLI_SESSION_SOURCE_TYPE, REMOTE_SESSION_SOURCE_TYPE))).order_by(desc(AgentRun.created_at), desc(AgentRun.id)).limit(1))).scalars().first()
        work = None
        if run is not None and getattr(run, "work_item_id", None) is not None:
            work = (await session.execute(select(AgentWorkItem).where(AgentWorkItem.id == run.work_item_id, AgentWorkItem.source_type.in_((CLI_SESSION_SOURCE_TYPE, REMOTE_SESSION_SOURCE_TYPE)), AgentWorkItem.execution_adapter.in_((CLI_SESSION_ADAPTER_KEY, MUSE_REMOTE_ADAPTER_KEY))))).scalars().first()
        if work is None:
            work = (await session.execute(select(AgentWorkItem).where(AgentWorkItem.conversation_session_id == session_id, AgentWorkItem.source_type.in_((CLI_SESSION_SOURCE_TYPE, REMOTE_SESSION_SOURCE_TYPE)), AgentWorkItem.execution_adapter.in_((CLI_SESSION_ADAPTER_KEY, MUSE_REMOTE_ADAPTER_KEY))).order_by(desc(AgentWorkItem.created_at), desc(AgentWorkItem.id)).limit(1))).scalars().first()
        return run, work

    @staticmethod
    def _remote_result_fields(run: Any | None, work: Any | None) -> dict[str, Any]:
        run_result = (
            getattr(run, "result", None)
            if isinstance(getattr(run, "result", None), Mapping)
            else {}
        )
        work_result = (
            getattr(work, "result_summary_json", None)
            if isinstance(getattr(work, "result_summary_json", None), Mapping)
            else {}
        )
        turn_status = run_result.get("turn_status") or work_result.get("turn_status")
        delivery = run_result.get("delivery") or work_result.get("delivery")
        return {
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "turn_status": (
                str(turn_status)[:32]
                if isinstance(turn_status, str) and turn_status
                else None
            ),
            "delivery": dict(delivery) if isinstance(delivery, Mapping) else None,
        }

    async def _binding_for_row(self, session: Any, row: Any) -> Any:
        context = self._context(row)
        binding_id = context.get("binding_id")
        revision_id = context.get("agent_revision_id")
        agent_id = context.get("agent_id") or getattr(row, "agent_id", None)
        if not binding_id or not agent_id or not revision_id:
            raise CLIDevelopmentSessionConflict("immutable Agent binding is missing", code="agent_binding_required")
        resolver = getattr(self.agent_service, "resolve_execution_binding_by_id", None)
        if callable(resolver):
            return await resolver(
                binding_id=binding_id,
                session_id=str(row.id),
                agent_id=agent_id,
                agent_revision_id=revision_id,
                db_session=session,
            )
        binding = await session.get(AgentExecutionBinding, uuid.UUID(str(binding_id)))
        if binding is None:
            raise CLIDevelopmentSessionConflict("immutable Agent binding is missing", code="agent_binding_required")
        return AgentExecutionBindingSnapshot.from_row(binding)

    @staticmethod
    def _managed_role_mapping(binding: Any, context: Mapping[str, Any]) -> dict[str, Any] | None:
        if str(context.get("role_context_mode") or "raw") != "agent_role":
            return None
        role = _build_pinned_role_context(binding, context)
        return {
            "mode": role.mode,
            "context_schema_version": role.context_schema_version,
            "agent_id": role.agent_id,
            # Muse's provider-only contract names this immutable pin
            # ``agent_revision_id``.  Keep the role text in memory only; the
            # returned mapping is used to build an ephemeral claim below.
            "agent_revision_id": role.agent_revision_id,
            "role_context_sha256": role.role_context_sha256,
            "text": role.text,
        }

    async def _create_remote_session(self, req: CreateCLIDevelopmentSessionRequest, *, session_id: uuid.UUID, wire_fingerprint: str) -> dict[str, Any]:
        """Admit one typed Muse route without entering local CLI policy."""

        self._assert_remote_admission(owner_id=req.owner_id, agent_id=req.agent_id)
        try:
            agent, revision = await self.agent_service.validate_agent_selection(
                agent_id=req.agent_id,
                actor_user_id=req.owner_id,
                actor_role=req.actor_role,
                project_id=req.project_id,
            )
        except CLIDevelopmentSessionError:
            raise
        except Exception as exc:
            raise CLIDevelopmentSessionForbidden("Agent selection is not authorized", code="agent_selection_denied") from exc
        agent_id = str(getattr(agent, "id", None) or req.agent_id)
        revision_id = str(getattr(revision, "id", None) or "")
        if req.agent_revision_id is not None and revision_id != req.agent_revision_id:
            raise CLIDevelopmentSessionConflict(
                "Requested AgentRevision is stale", code="agent_revision_stale"
            )
        pin_turn_binding = getattr(self.agent_service, "pin_turn_binding", None)
        if not callable(pin_turn_binding):
            raise CLIDevelopmentSessionUnavailable(
                "Atomic remote AgentRevision admission is unavailable",
                code="agent_revision_pin_unavailable",
            )
        try:
            expected_snapshot = AgentExecutionBindingSnapshot.from_revision(
                session_id=session_id,
                agent=agent,
                revision=revision,
                config=self.config,
                created_by=req.owner_id,
                execution_profile_id=req.execution_profile_id,
            )
            route = _assert_route(expected_snapshot, provider=MUSE_PROVIDER, model=None, effort=None)
            role_context = (
                _build_pinned_role_context(expected_snapshot)
                if req.role_context_mode == "agent_role"
                else None
            )
            if role_context is not None:
                wire_fingerprint = self._wire_create_fingerprint(
                    req,
                    role_context_sha256=role_context.role_context_sha256,
                    resolved_agent_revision_id=revision_id,
                )
        except CLIDevelopmentSessionError:
            raise
        except ConversationAgentValidationError as exc:
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        except Exception as exc:
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        authority = await self._resolve_authority(
            agent_id=agent_id,
            revision_id=revision_id,
            project_id=req.project_id,
            capabilities=REMOTE_SESSION_CAPABILITIES,
        )
        context_tuple = self._context_tuple(
            owner_id=req.owner_id,
            agent_id=agent_id,
            provider=MUSE_PROVIDER,
            model=None,
            effort="",
            working_directory=None,
            idempotency_key=req.idempotency_key,
            project_id=req.project_id,
            permission_mode=None,
            title=req.title,
            allowed_capabilities=REMOTE_SESSION_CAPABILITIES,
            execution_target=MUSE_EXECUTION_TARGET,
        )
        context_tuple["agent_revision_id"] = revision_id
        # Muse has no local native session handle.  The shared context tuple
        # keeps the legacy local default; remove it only for this remote lane.
        context_tuple.pop("native_state", None)
        context_tuple.update(
            {
                "wire_request_fingerprint": wire_fingerprint,
                "remote_target_key": str(session_id),
                "remote_binding_state": "unbound",
                "route": route,
                "authority": authority,
                "policy": {
                    "execution_target": MUSE_EXECUTION_TARGET,
                    "local_filesystem": False,
                    "sandbox": "remote_uncontrolled",
                },
            }
        )
        deployment_material = {
            "owner_id": req.owner_id,
            "agent_id": agent_id,
            "provider": MUSE_PROVIDER,
            "execution_target": MUSE_EXECUTION_TARGET,
            "project_id": req.project_id,
            "title": req.title,
            "agent_revision_id": revision_id,
            "allowed_capabilities": list(REMOTE_SESSION_CAPABILITIES),
            "route": route,
        }
        if req.execution_profile_id is not None:
            context_tuple["execution_profile_id"] = req.execution_profile_id
            deployment_material["execution_profile_id"] = req.execution_profile_id
        if role_context is not None:
            context_tuple.update(
                {
                    "role_context_mode": role_context.mode,
                    "role_context_schema_version": role_context.context_schema_version,
                    "role_context_agent_id": role_context.agent_id,
                    "role_context_agent_revision_id": role_context.agent_revision_id,
                    "role_context_sha256": role_context.role_context_sha256,
                }
            )
            deployment_material["role_context"] = {
                "mode": role_context.mode,
                "context_schema_version": role_context.context_schema_version,
                "agent_id": role_context.agent_id,
                "agent_revision_id": role_context.agent_revision_id,
                "role_context_sha256": role_context.role_context_sha256,
            }
        context_tuple["deployment_fingerprint"] = _json_hash(deployment_material)
        context = {CLI_DEVELOPMENT_CONTEXT_KEY: context_tuple}
        policy = cli_development_policy(self.config)

        async def validated_receipt(db: Any, row: Any) -> dict[str, Any]:
            current = self._context(row)
            if (
                getattr(row, "deleted_at", None) is not None
                or str(getattr(row, "user_id", "")) != req.owner_id
                or not self._wire_create_fingerprint_matches(
                    req, current, wire_fingerprint
                )
            ):
                raise CLIDevelopmentSessionConflict(
                    "session idempotency key was reused with a different request",
                    code="session_idempotency_conflict",
                )
            if str(current.get("admission_state") or "") != "ready":
                raise CLIDevelopmentSessionConflict(
                    "remote session admission is incomplete; use a new idempotency key",
                    code="session_admission_incomplete",
                )
            if (
                str(current.get("agent_id") or "") != agent_id
                or str(current.get("agent_revision_id") or "") != revision_id
                or str(getattr(row, "agent_id", "") or "") != agent_id
                or str(current.get("execution_profile_id") or "")
                != str(req.execution_profile_id or "")
                or str(current.get("role_context_mode") or "raw")
                != req.role_context_mode
                or str(current.get("role_context_sha256") or "")
                != str(role_context.role_context_sha256 if role_context else "")
            ):
                raise CLIDevelopmentSessionConflict(
                    "session idempotency key was reused with a different fixed Agent context",
                    code="session_idempotency_conflict",
                )
            binding = await self._binding_for_row(db, row)
            if str(_binding_value(binding, "agent_revision_id", "")) != revision_id:
                raise CLIDevelopmentSessionConflict(
                    "remote session binding revision changed",
                    code="agent_revision_stale",
                )
            bound_route = _assert_route(
                binding, provider=MUSE_PROVIDER, model=None, effort=None
            )
            _assert_binding_selection(binding, req.execution_profile_id)
            if role_context is not None:
                _build_pinned_role_context(binding, current)
            binding_id = str(
                _binding_value(binding, "binding_id")
                or _binding_value(binding, "id")
                or ""
            )
            binding_hash = str(_binding_value(binding, "snapshot_hash") or "")
            if (
                not binding_id
                or not binding_hash
                or str(current.get("binding_id") or "") != binding_id
                or str(current.get("binding_snapshot_hash") or "") != binding_hash
                or current.get("route") != bound_route
            ):
                raise CLIDevelopmentSessionConflict(
                    "remote session binding proof is incomplete",
                    code="agent_binding_proof_missing",
                )
            return await self._session_projection(
                db, row, policy=policy, owner_id=req.owner_id, created=False
            )

        db = await self._session()
        try:
            existing = (await db.execute(select(ConversationSession).where(ConversationSession.id == session_id).with_for_update())).scalars().first()
            if existing is not None:
                return await validated_receipt(db, existing)
            row = ConversationSession(
                id=session_id,
                user_id=req.owner_id,
                character_name="cli_development",
                title=req.title,
                context=context,
                message_count=0,
                is_active=True,
                project_id=uuid.UUID(req.project_id) if req.project_id else None,
                agent_id=uuid.UUID(agent_id),
                session_start=self._clock(),
                last_activity=self._clock(),
                development_status="waiting_for_user",
            )
            db.add(row)
            db.add(ConversationParticipant(session_id=session_id, participant_type="user", participant_id=req.owner_id, role="owner", status="joined", auto_respond=False, participant_metadata={}))
            await db.flush()

            # Persist the canonical Agent binding in the same transaction as
            # the deterministic session row. A crash cannot leave a committed
            # "creating" receipt that a duplicate request could mistake for a
            # bound session.
            binding = await pin_turn_binding(
                db_session=db,
                session_id=str(session_id),
                agent_id=agent_id,
                agent_revision_id=revision_id,
                actor_user_id=req.owner_id,
                actor_role=req.actor_role,
                config=self.config,
                execution_profile_id=req.execution_profile_id,
            )
            if str(_binding_value(binding, "agent_revision_id", "")) != revision_id:
                raise CLIDevelopmentSessionConflict(
                    "Exact AgentRevision binding is unavailable",
                    code="agent_revision_binding_required",
                )
            bound_route = _assert_route(
                binding, provider=MUSE_PROVIDER, model=None, effort=None
            )
            _assert_binding_selection(binding, req.execution_profile_id)
            if role_context is not None:
                _build_pinned_role_context(binding, context_tuple)
            binding_id = str(
                _binding_value(binding, "binding_id")
                or _binding_value(binding, "id")
                or ""
            )
            binding_hash = str(_binding_value(binding, "snapshot_hash") or "")
            if not binding_id or not binding_hash:
                raise CLIDevelopmentSessionConflict(
                    "immutable Agent binding proof is incomplete",
                    code="agent_binding_proof_missing",
                )
            ready_context = copy.deepcopy(context_tuple)
            ready_context.update(
                {
                    "binding_id": binding_id,
                    "binding_snapshot_hash": binding_hash,
                    "route": bound_route,
                    "status": "ready",
                    "admission_state": "ready",
                }
            )
            row.context = {CLI_DEVELOPMENT_CONTEXT_KEY: ready_context}
            await db.commit()
            return await self._session_projection(
                db, row, policy=policy, owner_id=req.owner_id, created=True
            )
        except IntegrityError as exc:
            await self._rollback_async(db)
            if not _is_unique_integrity_error(exc):
                raise CLIDevelopmentSessionStorageError("remote session admission could not be persisted", code="session_admission_storage_error") from None
            winner_db = await self._session()
            try:
                winner = await winner_db.get(ConversationSession, session_id)
                if winner is None:
                    raise CLIDevelopmentSessionConflict("remote session admission could not be reconciled", code="session_admission_unavailable")
                return await validated_receipt(winner_db, winner)
            finally:
                await self._close(winner_db)
        except CLIDevelopmentSessionError:
            await self._rollback_async(db)
            raise
        except ConversationAgentConflictError as exc:
            await self._rollback_async(db)
            raise CLIDevelopmentSessionConflict(
                "Agent binding admission is stale",
                code=str(getattr(exc, "code", "") or "agent_revision_stale"),
            ) from exc
        except ConversationAgentValidationError as exc:
            await self._rollback_async(db)
            code = str(getattr(exc, "code", "") or "")
            raise CLIDevelopmentSessionConflict(
                "Agent binding admission failed",
                code=code or "agent_binding_required",
            ) from exc
        except Exception as exc:
            await self._rollback_async(db)
            raise CLIDevelopmentSessionForbidden(
                "Agent binding could not be persisted", code="agent_binding_denied"
            ) from exc
        finally:
            await self._close(db)

    async def create_session(self, request: Any = None, **kwargs: Any) -> dict[str, Any]:
        req = CreateCLIDevelopmentSessionRequest.from_value(request, **kwargs)
        wire_fingerprint = self._wire_create_fingerprint(req)
        session_id = self._session_identity(req.owner_id, req.idempotency_key)
        # A committed ready receipt is returned from its wire identity before
        # re-resolving mutable deployment policy/Agent state.  This is the
        # lost-response/idempotency path; creating admission remains eligible
        # for the guarded reconciliation below.
        early_db = await self._session()
        try:
            existing = (await early_db.execute(select(ConversationSession).where(ConversationSession.id == session_id).with_for_update())).scalars().first()
            if existing is not None:
                current = self._context(existing)
                if getattr(existing, "deleted_at", None) is not None:
                    raise CLIDevelopmentSessionConflict("session idempotency key is tombstoned", code="session_idempotency_conflict")
                if str(getattr(existing, "user_id", "")) != req.owner_id:
                    raise CLIDevelopmentSessionConflict("session idempotency key was reused with a different request", code="session_idempotency_conflict")
                # Managed contexts need a freshly resolved immutable Revision
                # and digest before replay. Muse also resolves its server-owned
                # route and binding atomically before returning a receipt.
                if (
                    req.role_context_mode != "agent_role"
                    and req.provider != MUSE_PROVIDER
                    and req.agent_revision_id is None
                ):
                    if not self._wire_create_fingerprint_matches(
                        req, current, wire_fingerprint
                    ):
                        raise CLIDevelopmentSessionConflict("session idempotency key was reused with a different request", code="session_idempotency_conflict")
                    if str(current.get("admission_state") or "") == "ready":
                        return await self._session_projection(early_db, existing, policy=cli_development_policy(self.config), owner_id=req.owner_id, created=False)
        finally:
            await self._close(early_db)
        if req.provider == MUSE_PROVIDER:
            return await self._create_remote_session(
                req,
                session_id=session_id,
                wire_fingerprint=wire_fingerprint,
            )
        policy, cwd = self._policy_for_request(owner_id=req.owner_id, agent_id=req.agent_id, working_directory=req.working_directory)
        permission_mode = self._resolve_permission_mode(
            policy, req.provider, req.permission_mode
        )

        # Validate the requested Agent and route before mutating a session.
        try:
            agent, revision = await self.agent_service.validate_agent_selection(
                agent_id=req.agent_id,
                actor_user_id=req.owner_id,
                actor_role=req.actor_role,
                project_id=req.project_id,
            )
        except CLIDevelopmentSessionError:
            raise
        except Exception as exc:
            raise CLIDevelopmentSessionForbidden("Agent selection is not authorized", code="agent_selection_denied") from exc
        revision_id = str(getattr(revision, "id", None) or "")
        agent_id = str(getattr(agent, "id", None) or req.agent_id)
        if req.agent_revision_id is not None and revision_id != req.agent_revision_id:
            raise CLIDevelopmentSessionConflict(
                "Requested AgentRevision is stale", code="agent_revision_stale"
            )
        effective_revision_id = req.agent_revision_id
        if effective_revision_id is None and req.role_context_mode == "agent_role":
            if not revision_id:
                raise CLIDevelopmentSessionConflict(
                    "Validated AgentRevision is unavailable",
                    code="agent_revision_binding_required",
                )
            # The validated revision becomes this admission's immutable pin;
            # the caller request stays untouched for wire identity.
            effective_revision_id = revision_id
        if effective_revision_id is not None and not callable(
            getattr(self.agent_service, "pin_turn_binding", None)
        ):
            raise CLIDevelopmentSessionUnavailable(
                "Exact AgentRevision admission is unavailable",
                code="agent_revision_pin_unavailable",
            )
        # Build the expected immutable snapshot once. bind_session_agent will
        # create the persisted row and is checked again after the lock.
        role_context: AgentRoleContext | None = None
        try:
            expected_snapshot = AgentExecutionBindingSnapshot.from_revision(
                session_id=session_id,
                agent=agent,
                revision=revision,
                config=self.config,
                created_by=req.owner_id,
                execution_profile_id=req.execution_profile_id,
            )
            route = _assert_route(expected_snapshot, provider=req.provider, model=req.model, effort=req.effort)
            if req.role_context_mode == "agent_role":
                role_context = _build_pinned_role_context(expected_snapshot)
                wire_fingerprint = self._wire_create_fingerprint(
                    req,
                    role_context_sha256=role_context.role_context_sha256,
                    resolved_agent_revision_id=effective_revision_id,
                )
        except CLIDevelopmentSessionError:
            raise
        except ConversationAgentValidationError as exc:
            code = str(getattr(exc, "code", "") or "")
            if req.execution_profile_id is not None and code.startswith("agent_execution_profile_"):
                raise CLIDevelopmentSessionInvalid("ExecutionProfile selection is not allowed", code=code) from None
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        except Exception as exc:
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        if req.allowed_capabilities != CLI_DEVELOPMENT_CAPABILITIES and req.provider not in {"claude", "claude-cli", "claudecode"}:
            raise CLIDevelopmentSessionInvalid("narrowed capabilities are only enforceable for the Claude provider", code="allowed_capabilities_unsupported_provider")
        authority_revision_id = effective_revision_id or revision_id
        authority = await self._resolve_authority(agent_id=agent_id, revision_id=authority_revision_id, project_id=req.project_id, capabilities=req.allowed_capabilities)
        context_tuple = self._context_tuple(owner_id=req.owner_id, agent_id=agent_id, provider=req.provider, model=req.model, effort=req.effort, working_directory=cwd, idempotency_key=req.idempotency_key, project_id=req.project_id, permission_mode=permission_mode, title=req.title, allowed_capabilities=req.allowed_capabilities)
        context_tuple["provider_cache_root"] = self._provider_cache_root(policy, req.provider)
        context_tuple["wire_request_fingerprint"] = wire_fingerprint
        deployment_material = {
            "owner_id": req.owner_id,
            "agent_id": agent_id,
            "provider": req.provider,
            "model": req.model,
            "effort": req.effort,
            "working_directory": cwd,
            "project_id": req.project_id,
            "permission_mode": permission_mode,
            "title": req.title,
            "allowed_capabilities": list(req.allowed_capabilities),
            "provider_cache_root": context_tuple["provider_cache_root"],
        }
        if req.execution_profile_id is not None:
            context_tuple["execution_profile_id"] = req.execution_profile_id
            deployment_material["execution_profile_id"] = req.execution_profile_id
        if effective_revision_id is not None:
            context_tuple["agent_revision_id"] = revision_id
            deployment_material["agent_revision_id"] = revision_id
        if role_context is not None:
            context_tuple.update(
                {
                    "role_context_mode": role_context.mode,
                    "role_context_schema_version": role_context.context_schema_version,
                    "role_context_agent_id": role_context.agent_id,
                    "role_context_agent_revision_id": role_context.agent_revision_id,
                    "role_context_sha256": role_context.role_context_sha256,
                }
            )
            deployment_material["role_context"] = {
                "mode": role_context.mode,
                "context_schema_version": role_context.context_schema_version,
                "agent_id": role_context.agent_id,
                "agent_revision_id": role_context.agent_revision_id,
                "role_context_sha256": role_context.role_context_sha256,
            }
        context_tuple["deployment_fingerprint"] = _json_hash(deployment_material)
        context_tuple["route"] = route
        context_tuple["policy"] = policy.to_diagnostic(cwd=cwd, owner_id=req.owner_id)
        context_tuple["authority"] = authority
        context = {CLI_DEVELOPMENT_CONTEXT_KEY: context_tuple}

        async def exact_receipt_binding(db: Any, row: Any, current: Mapping[str, Any]) -> Any:
            if str(current.get("admission_state") or "") != "ready":
                raise CLIDevelopmentSessionConflict(
                    "Exact AgentRevision admission is incomplete",
                    code="session_admission_incomplete",
                )
            if (
                str(current.get("agent_revision_id") or "") != str(effective_revision_id)
                or str(getattr(row, "agent_id", "") or "") != agent_id
            ):
                raise CLIDevelopmentSessionConflict(
                    "session idempotency key was reused with a different exact Agent context",
                    code="session_idempotency_conflict",
                )
            try:
                exact_binding = await self._binding_for_row(db, row)
            except Exception as exc:
                raise CLIDevelopmentSessionConflict(
                    "immutable exact Agent binding is unavailable",
                    code="agent_revision_binding_required",
                ) from exc
            if str(_binding_value(exact_binding, "agent_revision_id", "")) != str(effective_revision_id):
                raise CLIDevelopmentSessionConflict(
                    "session idempotency key has a different AgentRevision binding",
                    code="session_idempotency_conflict",
                )
            exact_route = _assert_route(
                exact_binding, provider=req.provider, model=req.model, effort=req.effort
            )
            _assert_binding_selection(exact_binding, req.execution_profile_id)
            if role_context is not None:
                _build_pinned_role_context(exact_binding, current)
            exact_binding_id = str(
                _binding_value(exact_binding, "binding_id")
                or _binding_value(exact_binding, "id")
                or ""
            )
            exact_binding_hash = str(_binding_value(exact_binding, "snapshot_hash") or "")
            if (
                not exact_binding_id
                or not exact_binding_hash
                or str(current.get("binding_id") or "") != exact_binding_id
                or str(current.get("binding_snapshot_hash") or "") != exact_binding_hash
                or current.get("route") != exact_route
                or exact_route != route
            ):
                raise CLIDevelopmentSessionConflict(
                    "immutable exact Agent binding proof is incomplete or changed",
                    code="agent_binding_proof_missing",
                )
            return exact_binding

        db = await self._session()
        try:
            existing = (await db.execute(select(ConversationSession).where(ConversationSession.id == session_id).with_for_update())).scalars().first()
            if existing is not None:
                current = self._context(existing)
                if str(getattr(existing, "user_id", "")) != req.owner_id or current.get("idempotency_digest") != context_tuple["idempotency_digest"] or not self._wire_create_fingerprint_matches(req, current, wire_fingerprint):
                    raise CLIDevelopmentSessionConflict("session idempotency key belongs to another request", code="session_idempotency_conflict")
                for fixed_key in ("agent_id", "provider", "model", "effort", "working_directory", "project_id", "permission_mode", "execution_profile_id", "role_context_mode", "role_context_sha256"):
                    if str(current.get(fixed_key) or "") != str(context_tuple.get(fixed_key) or ""):
                        raise CLIDevelopmentSessionConflict("session idempotency key was reused with a different fixed route", code="session_idempotency_conflict")
                if effective_revision_id is not None:
                    await exact_receipt_binding(db, existing, current)
                    return await self._session_projection(db, existing, policy=policy, owner_id=req.owner_id, created=False)
                binding = None
                try:
                    binding = await self._binding_for_row(db, existing)
                except Exception:
                    if str(current.get("admission_state") or "") != "creating":
                        raise CLIDevelopmentSessionConflict("immutable Agent binding is unavailable", code="agent_binding_required")
                    if effective_revision_id is not None:
                        # Exact-revision admission is atomic below. A committed
                        # creating row has no safe canonical exact binding to
                        # recover and must never fall back to latest.
                        raise CLIDevelopmentSessionConflict(
                            "Exact AgentRevision binding is unavailable",
                            code="agent_revision_binding_required",
                        )
                    # A process may have committed the deterministic session
                    # row immediately before crashing in the separate
                    # canonical bind transaction. Release this lock before
                    # retrying the idempotent bind; the next caller can
                    # safely complete admission instead of seeing ready.
                    await db.commit()
                    await self._close(db)
                    db = await self._session()
                    bind_result = await self.agent_service.bind_session_agent(
                        session_id=str(existing.id),
                        agent_id=str(current.get("agent_id") or agent_id),
                        actor_user_id=req.owner_id,
                        actor_role=req.actor_role,
                        expected_agent_id=str(existing.agent_id) if existing.agent_id else None,
                        **_execution_profile_kwargs(current.get("execution_profile_id")),
                    )
                    binding = getattr(bind_result, "binding", None)
                    await self._close(db)
                    db = await self._session()
                    existing = (await db.execute(select(ConversationSession).where(ConversationSession.id == session_id).with_for_update())).scalars().first()
                    if existing is None:
                        raise CLIDevelopmentSessionNotFound()
                    if binding is None:
                        raise CLIDevelopmentSessionConflict("immutable Agent binding is unavailable", code="agent_binding_required")
                _assert_route(binding, provider=req.provider, model=req.model, effort=req.effort)
                _assert_binding_selection(binding, current.get("execution_profile_id"))
                if role_context is not None:
                    _build_pinned_role_context(binding, current)
                binding_id = str(_binding_value(binding, "binding_id") or _binding_value(binding, "id") or "")
                binding_hash = str(_binding_value(binding, "snapshot_hash") or "")
                if not binding_id or not binding_hash:
                    raise CLIDevelopmentSessionConflict("immutable Agent binding proof is incomplete", code="agent_binding_proof_missing")
                if str(current.get("admission_state") or "") == "creating" or not current.get("binding_id"):
                    current.update({"binding_id": binding_id, "binding_snapshot_hash": binding_hash, "agent_revision_id": revision_id, "route": route, "status": "ready", "admission_state": "ready"})
                    next_context = copy.deepcopy(existing.context) if isinstance(existing.context, Mapping) else {}
                    next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = current
                    existing.context = next_context
                    await db.commit()
                return await self._session_projection(db, existing, policy=policy, owner_id=req.owner_id, created=False)
            row = ConversationSession(
                id=session_id,
                user_id=req.owner_id,
                character_name="cli_development",
                title=req.title,
                context=context,
                message_count=0,
                is_active=True,
                project_id=uuid.UUID(req.project_id) if req.project_id else None,
                # Bind through ConversationAgentService after the row exists;
                # setting this before its CAS path would make
                # ``expected_agent_id=None`` look stale.
                agent_id=(uuid.UUID(agent_id) if effective_revision_id is not None else None),
                session_start=self._clock(),
                last_activity=self._clock(),
                development_status="waiting_for_user",
            )
            db.add(row)
            db.add(ConversationParticipant(session_id=session_id, participant_type="user", participant_id=req.owner_id, role="owner", status="joined", auto_respond=False, participant_metadata={}))
            await db.flush()
            if effective_revision_id is not None:
                exact_binding = await self.agent_service.pin_turn_binding(
                    db_session=db,
                    session_id=str(session_id),
                    agent_id=agent_id,
                    agent_revision_id=effective_revision_id,
                    actor_user_id=req.owner_id,
                    actor_role=req.actor_role,
                    config=self.config,
                    execution_profile_id=req.execution_profile_id,
                )
                exact_route = _assert_route(
                    exact_binding,
                    provider=req.provider,
                    model=req.model,
                    effort=req.effort,
                )
                _assert_binding_selection(exact_binding, req.execution_profile_id)
                if role_context is not None:
                    _build_pinned_role_context(exact_binding, context_tuple)
                binding_id = str(
                    _binding_value(exact_binding, "binding_id")
                    or _binding_value(exact_binding, "id")
                    or ""
                )
                binding_hash = str(_binding_value(exact_binding, "snapshot_hash") or "")
                if (
                    str(_binding_value(exact_binding, "agent_revision_id") or "")
                    != effective_revision_id
                    or not binding_id
                    or not binding_hash
                ):
                    raise CLIDevelopmentSessionConflict(
                        "Exact AgentRevision binding proof is incomplete",
                        code="agent_revision_binding_required",
                    )
                ready_context = copy.deepcopy(context_tuple)
                ready_context.update(
                    {
                        "binding_id": binding_id,
                        "binding_snapshot_hash": binding_hash,
                        "agent_revision_id": effective_revision_id,
                        "route": exact_route,
                        "status": "ready",
                        "admission_state": "ready",
                    }
                )
                row.context = {CLI_DEVELOPMENT_CONTEXT_KEY: ready_context}
            await db.commit()
            if effective_revision_id is not None:
                return await self._session_projection(
                    db, row, policy=policy, owner_id=req.owner_id, created=True
                )
        except CLIDevelopmentSessionError:
            await self._rollback_async(db)
            raise
        except ConversationAgentConflictError as exc:
            await self._rollback_async(db)
            raise CLIDevelopmentSessionConflict(
                "Agent binding admission is stale",
                code=str(getattr(exc, "code", "") or "agent_revision_stale"),
            ) from exc
        except ConversationAgentValidationError as exc:
            await self._rollback_async(db)
            code = str(getattr(exc, "code", "") or "")
            raise CLIDevelopmentSessionConflict(
                "Agent binding admission failed",
                code=code or "agent_binding_required",
            ) from exc
        except IntegrityError as exc:
            await self._rollback_async(db)
            if not _is_unique_integrity_error(exc):
                raise CLIDevelopmentSessionStorageError("session admission could not be persisted", code="session_admission_storage_error") from None
            # The deterministic PK is the cross-process creation fence. Read
            # the winner and return it only after comparing the owner/digest.
            winner_db = await self._session()
            try:
                winner = await winner_db.get(ConversationSession, session_id)
                if winner is None:
                    raise exc
                current = self._context(winner)
                if str(getattr(winner, "user_id", "")) != req.owner_id or current.get("idempotency_digest") != context_tuple["idempotency_digest"] or not self._wire_create_fingerprint_matches(req, current, wire_fingerprint):
                    raise CLIDevelopmentSessionConflict("session idempotency key conflict", code="session_idempotency_conflict")
                for fixed_key in ("agent_id", "provider", "model", "effort", "working_directory", "project_id", "permission_mode", "execution_profile_id", "role_context_mode", "role_context_sha256"):
                    if str(current.get(fixed_key) or "") != str(context_tuple.get(fixed_key) or ""):
                        raise CLIDevelopmentSessionConflict("session idempotency key was reused with a different fixed route", code="session_idempotency_conflict")
                if effective_revision_id is not None:
                    await exact_receipt_binding(winner_db, winner, current)
                return await self._session_projection(winner_db, winner, policy=policy, owner_id=req.owner_id, created=False)
            finally:
                await self._close(winner_db)
        finally:
            await self._close(db)

        # bind_session_agent owns its transaction so its immutable row is
        # protected by the canonical CAS/ACL path rather than a local copy.
        try:
            result = await self.agent_service.bind_session_agent(
                session_id=str(session_id), agent_id=agent_id, actor_user_id=req.owner_id, actor_role=req.actor_role, expected_agent_id=None,
                **_execution_profile_kwargs(req.execution_profile_id),
            )
        except Exception as exc:
            raise CLIDevelopmentSessionForbidden("Agent binding could not be persisted", code="agent_binding_denied") from exc
        binding = getattr(result, "binding", None)
        if binding is None:
            raise CLIDevelopmentSessionConflict("immutable Agent binding is missing", code="agent_binding_required")
        bound_route = _assert_route(binding, provider=req.provider, model=req.model, effort=req.effort)
        _assert_binding_selection(binding, req.execution_profile_id)
        if role_context is not None:
            _build_pinned_role_context(binding, context_tuple)
        binding_id = str(_binding_value(binding, "binding_id") or _binding_value(binding, "id") or "")
        binding_hash = str(_binding_value(binding, "snapshot_hash") or "")
        if not binding_id or not binding_hash:
            raise CLIDevelopmentSessionConflict("immutable Agent binding proof is incomplete", code="agent_binding_proof_missing")
        db = await self._session()
        try:
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == session_id).with_for_update())).scalars().first()
            if row is None:
                raise CLIDevelopmentSessionNotFound()
            current = self._context(row)
            current.update({"binding_id": binding_id, "binding_snapshot_hash": binding_hash, "agent_revision_id": revision_id, "route": bound_route, "status": "ready", "admission_state": "ready"})
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = current
            row.context = next_context
            await db.commit()
            return await self._session_projection(db, row, policy=policy, owner_id=req.owner_id, created=True)
        finally:
            await self._close(db)

    async def _session_projection(self, db: Any, row: Any, *, policy: CLIDevelopmentPolicy, owner_id: str, created: bool) -> dict[str, Any]:
        run, work = await self._load_latest(db, row.id)
        context = self._context(row)
        remote = self._is_remote_context(context)
        remote_fields = self._remote_result_fields(run, work) if remote else {}
        delivery = remote_fields.get("delivery")
        policy_projection = (
            {
                "execution_target": MUSE_EXECUTION_TARGET,
                "local_filesystem": False,
                "sandbox": "remote_uncontrolled",
            }
            if remote
            else policy.to_diagnostic(
                cwd=context.get("working_directory"), owner_id=owner_id
            )
        )
        return {
            "session_id": str(row.id),
            "owner_id": owner_id,
            "agent_id": context.get("agent_id") or str(getattr(row, "agent_id", "")),
            "agent_revision_id": context.get("agent_revision_id"),
            "binding_id": context.get("binding_id"),
            "binding_snapshot_hash": context.get("binding_snapshot_hash"),
            "provider": context.get("provider"),
            "model": context.get("model"),
            "effort": context.get("effort", ""),
            "working_directory": context.get("working_directory"),
            "role_context_mode": context.get("role_context_mode") or "raw",
            "role_context_sha256": context.get("role_context_sha256"),
            "execution_target": remote_fields.get("execution_target") or context.get("execution_target") or "local_cli",
            "local_filesystem": remote_fields.get("local_filesystem", True),
            "sandbox": remote_fields.get("sandbox") or "trusted_windows",
            "role_delivery": dict(delivery) if isinstance(delivery, Mapping) else None,
            "cancel_semantics": "local_stop_only_remote_unconfirmed" if remote else "local_provider_stop_requested",
            **(
                {
                    "turn_status": remote_fields.get("turn_status"),
                    "delivery": remote_fields.get("delivery"),
                }
                if remote
                else {}
            ),
            "status": self._status_from_rows(context, run, work),
            "created": created,
            "policy": policy_projection,
            "native_state": None if remote else context.get("native_state", "unknown"),
            "run": run.to_dict() if run is not None else None,
            "work": work.to_safe_dict() if work is not None else None,
        }

    async def get_session(self, session_id: Any, *, owner_id: str) -> dict[str, Any]:
        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        owner = _identifier(owner_id, field_name="owner_id")
        db = await self._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(sid))
            if row is None or getattr(row, "deleted_at", None) is not None:
                raise CLIDevelopmentSessionNotFound()
            if str(row.user_id) != owner:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            policy = cli_development_policy(self.config)
            return await self._session_projection(db, row, policy=policy, owner_id=owner, created=False)
        finally:
            await self._close(db)

    async def _send_remote_locked(
        self,
        db: Any,
        row: Any,
        context: Mapping[str, Any],
        request: SendCLIDevelopmentSessionRequest,
        *,
        sid: str,
        send_key: str,
        key_digest: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        """Create a remote Work item while the session row is locked."""

        self._assert_remote_admission(
            owner_id=request.owner_id,
            agent_id=str(context.get("agent_id") or row.agent_id or ""),
        )
        run, work = await self._load_latest(db, row.id)
        current_status = self._status_from_rows(context, run, work)
        if current_status == "cancelled":
            raise CLIDevelopmentSessionConflict(
                "cancelled remote session cannot accept another turn",
                code="session_binding_unsafe",
            )
        if current_status in {"queued", "running"}:
            raise CLIDevelopmentSessionConflict(
                "remote session already has a turn in flight",
                code="session_turn_in_flight",
            )
        if current_status in {"disconnected", "interrupted", "cancelling", "failed"}:
            raise CLIDevelopmentSessionUnavailable("remote session cannot accept a new send in its current state", code=f"session_{current_status}")
        session_context = row.context if isinstance(row.context, Mapping) else {}
        remote_context = session_context.get(MUSE_REMOTE_CONTEXT_KEY)
        if remote_context is not None:
            remote_binding = (
                remote_context.get("binding")
                if isinstance(remote_context, Mapping)
                and isinstance(remote_context.get("binding"), Mapping)
                else remote_context
            )
            if not isinstance(remote_binding, Mapping):
                raise CLIDevelopmentSessionConflict(
                    "remote thread binding cannot be confirmed safe",
                    code="session_binding_unsafe",
                )
            remote_state = str(remote_binding.get("state") or "")
            if (
                remote_state != "bound"
                or bool(remote_binding.get("current_turn_nonce"))
                or remote_binding.get("safe_to_send") is not True
            ):
                raise CLIDevelopmentSessionConflict(
                    "remote thread binding cannot accept another turn",
                    code="session_binding_unsafe",
                )
        binding = await self._binding_for_row(db, row)
        route = _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
        role_mapping = self._managed_role_mapping(binding, context)
        pinned_caps = REMOTE_SESSION_CAPABILITIES
        await self._resolve_authority(
            agent_id=str(context.get("agent_id") or row.agent_id or ""),
            revision_id=str(context.get("agent_revision_id") or ""),
            project_id=context.get("project_id"),
            capabilities=pinned_caps,
        )
        existing_message = (await db.execute(select(ConversationMessage).where(ConversationMessage.session_id == row.id, ConversationMessage.client_message_id == send_key).with_for_update())).scalars().first()
        if existing_message is not None:
            if existing_message.content != request.content:
                raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
            trigger_message_id = existing_message.id
        else:
            trigger_message_id = uuid.uuid4()
            db.add(ConversationMessage(id=trigger_message_id, session_id=row.id, role="user", content=request.content, client_message_id=send_key, sender_type="user", sender_id=request.owner_id, message_metadata={"client_message_id": send_key, "cli_development": True, "execution_target": MUSE_EXECUTION_TARGET}, created_at=self._clock(), updated_at=self._clock(), is_active_branch=True, branch_index=0))
            await db.flush()
            row.message_count = int(row.message_count or 0) + 1
            row.last_activity = self._clock()
            row.development_status = "working"
        run_id = uuid.uuid4()
        role_pins = {
            key: role_mapping[key]
            for key in (
                "mode",
                "context_schema_version",
                "agent_id",
                "agent_revision_id",
                "role_context_sha256",
            )
        } if role_mapping is not None else None
        manifest = {
            "schema_version": 2,
            "agent_id": str(context.get("agent_id") or row.agent_id),
            "agent_revision_id": str(context.get("agent_revision_id") or ""),
            "work_item_id": None,
            "source": REMOTE_SESSION_SOURCE_TYPE,
            "capabilities": list(pinned_caps),
            "route_snapshot": {"operator": dict(route)},
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
        }
        manifest.update(_binding_selection_manifest_fields(binding, context.get("execution_profile_id")))
        if role_pins is not None:
            manifest["role_context"] = dict(role_pins)
        manifest_hash = _json_hash(manifest)
        remote_run_metadata = {
            "schema_version": CLI_DEVELOPMENT_SCHEMA_VERSION,
            "send_key_digest": key_digest,
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "binding_id": context.get("binding_id"),
            "binding_snapshot_hash": context.get("binding_snapshot_hash"),
            "remote_target_key": sid,
        }
        if role_pins is not None:
            remote_run_metadata["role_context"] = dict(role_pins)
        run = AgentRun(
            id=run_id,
            root_run_id=run_id,
            session_id=row.id,
            trigger_message_id=trigger_message_id,
            agent_id=uuid.UUID(str(context.get("agent_id") or row.agent_id)),
            agent_revision_id=uuid.UUID(str(context.get("agent_revision_id"))),
            user_id=request.owner_id,
            client_message_id=send_key,
            client_message_key=key_digest,
            request_fingerprint=request_fingerprint,
            run_type=REMOTE_SESSION_SOURCE_TYPE,
            status="queued",
            provider=MUSE_PROVIDER,
            model=None,
            generation_profile="assisted_work",
            work_item_attempt=1,
            objective="",
            title="",
            result={},
            validation={},
            run_metadata={"cli_development": remote_run_metadata},
            resolved_execution_manifest=manifest,
            execution_manifest_hash=manifest_hash,
            created_at=self._clock(),
            updated_at=self._clock(),
            last_event_at=self._clock(),
        )
        db.add(run)
        await db.flush()
        work_metadata: dict[str, Any] = {
            "owner_id": request.owner_id,
            "execution_target": MUSE_EXECUTION_TARGET,
            "remote_target_key": sid,
            "binding_id": context.get("binding_id"),
            "binding_snapshot_hash": context.get("binding_snapshot_hash"),
        }
        if role_mapping is not None:
            # Keep role text out of durable Work/Run projections.  The common
            # remote adapter reconstructs it from the immutable Agent binding
            # in its in-memory claim immediately before provider execution.
            work_metadata["managed_role_context"] = {
                key: role_mapping[key]
                for key in (
                    "mode",
                    "context_schema_version",
                    "agent_id",
                    "agent_revision_id",
                    "role_context_sha256",
                )
            }
        work = AgentWorkItem(
            source_type=REMOTE_SESSION_SOURCE_TYPE,
            source_id=str(run_id),
            source_revision=str(context.get("agent_revision_id") or ""),
            intent_key=MUSE_REMOTE_ADAPTER_KEY,
            domain=CLI_DEVELOPMENT_DOMAIN,
            conversation_session_id=row.id,
            assigned_agent_id=run.agent_id,
            agent_revision_id=run.agent_revision_id,
            required_capabilities_json=list(pinned_caps),
            execution_adapter=MUSE_REMOTE_ADAPTER_KEY,
            max_attempts=1,
            state="pending",
            attempt_count=0,
            concurrency_key=f"conversation:{row.id}",
            metadata_json=work_metadata,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        db.add(work)
        await db.flush()
        run.work_item_id = work.id
        work.active_agent_run_id = run.id
        next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
        remote_context = dict(context)
        remote_context.update({"status": "queued", "last_run_id": str(run_id), "remote_binding_state": "pending"})
        next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = remote_context
        row.context = next_context
        manifest["work_item_id"] = str(work.id)
        run.resolved_execution_manifest = manifest
        run.execution_manifest_hash = _json_hash(manifest)
        queued_sequence = self._allocate_event_sequences(row, 1)[0]
        db.add(AgentRunEvent(run_id=run_id, sequence=1, event_type="run.queued", status="queued", message="Muse remote turn queued", payload={"source": REMOTE_SESSION_SOURCE_TYPE, "execution_target": MUSE_EXECUTION_TARGET, "cli_session_sequence": queued_sequence}, created_at=self._clock()))
        db.add(AgentWorkEvent(work_item_id=work.id, sequence=1, event_type="work.queued", from_state=None, to_state="pending", status="pending", actor_kind="service", actor_service_key="aoitalk.system", agent_run_id=run_id, payload_json={"source": REMOTE_SESSION_SOURCE_TYPE, "execution_target": MUSE_EXECUTION_TARGET}, created_at=self._clock()))
        await db.commit()
        return {"accepted": True, "created": True, "session_id": sid, "run_id": str(run_id), "work_item_id": str(work.id), "status": "queued", "execution_target": MUSE_EXECUTION_TARGET, "local_filesystem": False, "sandbox": "remote_uncontrolled", "run": run.to_dict(), "work": work.to_safe_dict()}

    async def send(self, session_id: Any, request: Any = None, **kwargs: Any) -> dict[str, Any]:
        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        req = SendCLIDevelopmentSessionRequest.from_value(request, **kwargs)
        db = await self._session()
        try:
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == uuid.UUID(sid), ConversationSession.deleted_at.is_(None)).with_for_update())).scalars().first()
            if row is None:
                raise CLIDevelopmentSessionNotFound()
            if str(row.user_id) != req.owner_id:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            context = self._context(row)
            if not context or str(context.get("schema_version")) != str(CLI_DEVELOPMENT_SCHEMA_VERSION):
                raise CLIDevelopmentSessionConflict("CLI session context is invalid", code="cli_session_context_invalid")
            # Deduplication is the first send decision after owner/context
            # validation. A completed/failed receipt with the same key must
            # be replayable; state checks apply only to a new key.
            send_key = req.send_key  # preserve exact identity; no strip
            key_digest = _sha256(send_key)
            request_fingerprint = _sha256(req.content)
            existing = (await db.execute(select(AgentRun).where(AgentRun.session_id == row.id, AgentRun.user_id == req.owner_id, AgentRun.client_message_key == key_digest).with_for_update())).scalars().first()
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
                return {"accepted": True, "created": False, "session_id": sid, "run": existing.to_dict(), "run_id": str(existing.id), "status": self._status_from_rows(context, existing, await db.get(AgentWorkItem, existing.work_item_id) if existing.work_item_id else None)}
            if self._is_remote_context(context):
                return await self._send_remote_locked(
                    db,
                    row,
                    context,
                    req,
                    sid=sid,
                    send_key=send_key,
                    key_digest=key_digest,
                    request_fingerprint=request_fingerprint,
                )
            current_status = self._status_from_rows(context, *(await self._load_latest(db, row.id)))
            if current_status in {"disconnected", "interrupted", "cancelling", "failed"}:
                raise CLIDevelopmentSessionUnavailable("session cannot accept a new send in its current state", code=f"session_{current_status}")
            if current_status == "cancelled" and str(context.get("native_state") or "") != "active":
                raise CLIDevelopmentSessionUnavailable("cancelled session has no confirmed native continuation", code="native_session_unavailable")
            if current_status == "cancelled":
                expected_scope = str(context.get("native_scope_key") or "")
                entries = (row.context or {}).get("cli_native_sessions") if isinstance(row.context, Mapping) else None
                continuation_ready = any(
                    isinstance(entry, Mapping)
                    and str(entry.get("scope_key") or "") == expected_scope
                    and str(entry.get("state") or "") == "active"
                    and str(entry.get("native_session_id") or "")
                    and not entry.get("active_generation_id")
                    and not entry.get("lease_expires_at")
                    for entry in (entries or [])
                )
                if not continuation_ready:
                    raise CLIDevelopmentSessionUnavailable("cancelled session native continuation lease is not free", code="native_session_unavailable")
            if current_status in {"waiting_input", "waiting_approval"} and str(context.get("native_state") or "") != "active":
                raise CLIDevelopmentSessionUnavailable("provider wait has no confirmed native continuation", code="native_session_unavailable")
            if current_status == "completed" and str(context.get("native_state") or "") != "active":
                # A successful result without a provider-confirmed handle is
                # a completed turn, but it is not a safe continuation point.
                raise CLIDevelopmentSessionUnavailable("native CLI session handle is not confirmed", code="native_session_unavailable")
            policy, cwd = self._policy_for_request(owner_id=req.owner_id, agent_id=str(context.get("agent_id") or row.agent_id), working_directory=str(context.get("working_directory") or ""))
            self._validate_permission_mode(policy, str(context.get("permission_mode") or "native_default"))
            expected_cache = self._provider_cache_root(policy, str(context.get("provider") or ""))
            if os.path.normcase(os.path.realpath(str(context.get("provider_cache_root") or ""))) != os.path.normcase(os.path.realpath(expected_cache)):
                raise CLIDevelopmentSessionUnavailable("provider state root changed", code="provider_state_root_changed")
            binding = await self._binding_for_row(db, row)
            route = _assert_route(binding, provider=str(context.get("provider") or ""), model=str(context.get("model") or ""), effort=str(context.get("effort") or ""))
            role_context_pins = pinned_cli_role_context(context)
            if role_context_pins is not None:
                _build_pinned_role_context(binding, context)
            pinned_caps = pinned_cli_capabilities(context)
            await self._resolve_authority(agent_id=str(context.get("agent_id") or row.agent_id), revision_id=str(context.get("agent_revision_id") or ""), project_id=context.get("project_id"), capabilities=pinned_caps)
            existing_message = (await db.execute(select(ConversationMessage).where(ConversationMessage.session_id == row.id, ConversationMessage.client_message_id == send_key).with_for_update())).scalars().first()
            if existing_message is not None:
                if existing_message.content != req.content:
                    raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
                trigger_message_id = existing_message.id
            else:
                trigger_message_id = uuid.uuid4()
                db.add(ConversationMessage(id=trigger_message_id, session_id=row.id, role="user", content=req.content, client_message_id=send_key, sender_type="user", sender_id=req.owner_id, message_metadata={"client_message_id": send_key, "cli_development": True}, created_at=self._clock(), updated_at=self._clock(), is_active_branch=True, branch_index=0))
                # AgentRun.trigger_message_id is an FK into conversation_messages.
                # Flush the new message before adding the Run so PostgreSQL's
                # UOW ordering cannot insert the Run first through the
                # ConversationSession/Run relationship cycle.
                await db.flush()
                row.message_count = int(row.message_count or 0) + 1
                row.last_activity = self._clock()
                row.development_status = "working"
            run_id = uuid.uuid4()
            manifest = {"schema_version": 2, "agent_id": str(context.get("agent_id") or row.agent_id), "agent_revision_id": str(context.get("agent_revision_id") or ""), "work_item_id": None, "source": CLI_SESSION_SOURCE_TYPE, "capabilities": list(pinned_caps), "route_snapshot": {"operator": dict(route)}}
            manifest.update(_binding_selection_manifest_fields(binding, context.get("execution_profile_id")))
            if role_context_pins is not None:
                manifest["role_context"] = dict(role_context_pins)
            manifest_hash = _json_hash(manifest)
            cli_run_metadata = {"schema_version": CLI_DEVELOPMENT_SCHEMA_VERSION, "send_key_digest": key_digest, "effort": str(context.get("effort") or ""), "permission_mode": str(context.get("permission_mode") or "native_default"), "approval_policy_source": "provider_native_default_unknown" if str(context.get("permission_mode") or "native_default") == "native_default" else "explicit_deployment_policy", "execution_lane": policy.execution_lane, "working_directory_fingerprint": _sha256(cwd), "provider_cache_root_fingerprint": _sha256(str(context.get("provider_cache_root") or "")), "binding_id": context.get("binding_id"), "binding_snapshot_hash": context.get("binding_snapshot_hash")}
            if role_context_pins is not None:
                cli_run_metadata["role_context"] = dict(role_context_pins)
            run = AgentRun(id=run_id, root_run_id=run_id, session_id=row.id, trigger_message_id=trigger_message_id, agent_id=uuid.UUID(str(context.get("agent_id") or row.agent_id)), agent_revision_id=uuid.UUID(str(context.get("agent_revision_id"))), user_id=req.owner_id, client_message_id=send_key, client_message_key=key_digest, request_fingerprint=request_fingerprint, run_type=CLI_SESSION_SOURCE_TYPE, status="queued", provider=str(context.get("provider") or ""), model=str(context.get("model") or ""), generation_profile="assisted_work", work_item_attempt=1, objective="", title="", result={}, validation={}, run_metadata={"cli_development": cli_run_metadata}, resolved_execution_manifest=manifest, execution_manifest_hash=manifest_hash, created_at=self._clock(), updated_at=self._clock(), last_event_at=self._clock())
            db.add(run)
            await db.flush()
            resource_key = f"conversation:{row.id}"
            work_metadata = {"owner_id": req.owner_id, "send_key_digest": key_digest, "provider": run.provider, "model": run.model, "effort": str(context.get("effort") or ""), "permission_mode": str(context.get("permission_mode") or "native_default"), "execution_lane": policy.execution_lane, "binding_id": context.get("binding_id"), "binding_snapshot_hash": context.get("binding_snapshot_hash")}
            if role_context_pins is not None:
                work_metadata["role_context"] = dict(role_context_pins)
            work =AgentWorkItem(source_type=CLI_SESSION_SOURCE_TYPE, source_id=str(run_id), source_revision=str(context.get("agent_revision_id") or ""), intent_key=CLI_SESSION_ADAPTER_KEY, domain=CLI_DEVELOPMENT_DOMAIN, conversation_session_id=row.id, assigned_agent_id=run.agent_id, agent_revision_id=run.agent_revision_id, required_capabilities_json=list(pinned_caps), execution_adapter=CLI_SESSION_ADAPTER_KEY, max_attempts=1, state="pending", attempt_count=0, concurrency_key=resource_key, metadata_json=work_metadata, created_at=self._clock(), updated_at=self._clock())
            db.add(work)
            await db.flush()
            run.work_item_id = work.id
            work.active_agent_run_id = run.id
            context["status"] = "queued"
            context["last_run_id"] = str(run_id)
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
            manifest["work_item_id"] = str(work.id)
            run.resolved_execution_manifest = manifest
            run.execution_manifest_hash = _json_hash(manifest)
            queued_sequence = self._allocate_event_sequences(row, 1)[0]
            db.add(AgentRunEvent(run_id=run_id, sequence=1, event_type="run.queued", status="queued", message="CLI session turn queued", payload={"source": CLI_SESSION_SOURCE_TYPE, "cli_session_sequence": queued_sequence}, created_at=self._clock()))
            db.add(AgentWorkEvent(work_item_id=work.id, sequence=1, event_type="work.queued", from_state=None, to_state="pending", status="pending", actor_kind="service", actor_service_key="aoitalk.system", agent_run_id=run_id, payload_json={"source": CLI_SESSION_SOURCE_TYPE}, created_at=self._clock()))
            await db.commit()
            return {"accepted": True, "created": True, "session_id": sid, "run_id": str(run_id), "work_item_id": str(work.id), "status": "queued", "run": run.to_dict(), "work": work.to_safe_dict()}
        except IntegrityError as exc:
            await self._rollback_async(db)
            if not _is_unique_integrity_error(exc):
                raise CLIDevelopmentSessionStorageError("CLI send could not be persisted", code="send_storage_error") from None
            probe = await self._session()
            try:
                winner = (await probe.execute(select(AgentRun).where(AgentRun.session_id == uuid.UUID(sid), AgentRun.user_id == req.owner_id, AgentRun.client_message_key == _sha256(req.send_key)).limit(1))).scalars().first()
                if winner is None:
                    raise CLIDevelopmentSessionConflict("send idempotency race could not be reconciled", code="send_idempotency_unavailable")
                if winner.request_fingerprint != _sha256(req.content):
                    raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
                return {"accepted": True, "created": False, "session_id": sid, "run_id": str(winner.id), "status": "queued", "run": winner.to_dict()}
            finally:
                await self._close(probe)
        finally:
            await self._close(db)

    async def list_events(self, session_id: Any, *, owner_id: str, after_cursor: str | None = None, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        owner = _identifier(owner_id, field_name="owner_id")
        limit = max(1, min(int(limit or 100), 500))
        token = after_cursor or cursor
        cursor_sequence = 0
        if token:
            try:
                raw = json.loads(base64.urlsafe_b64decode(str(token).encode("ascii") + b"===").decode("utf-8"))
                if not isinstance(raw, Mapping) or raw.get("version") != 3 or raw.get("session_id") != sid:
                    raise ValueError
                cursor_sequence = max(0, int(raw.get("sequence") or 0))
            except Exception as exc:
                raise CLIDevelopmentSessionInvalid("invalid event cursor", code="invalid_event_cursor") from exc
        db = await self._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(sid))
            if row is None or row.deleted_at is not None or str(row.user_id) != owner:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            events: list[dict[str, Any]] = []
            sequence_expr = AgentRunEvent.payload["cli_session_sequence"].as_integer()
            run_query = (
                select(AgentRunEvent)
                .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
                .where(
                    AgentRun.session_id == row.id,
                    AgentRun.run_type.in_((CLI_SESSION_SOURCE_TYPE, REMOTE_SESSION_SOURCE_TYPE)),
                    sequence_expr > cursor_sequence,
                )
                .order_by(sequence_expr, AgentRunEvent.id)
                .limit(limit + 1)
            )
            for item in (await db.execute(run_query)).scalars().all():
                event_payload = _safe_json(item.payload or {}) or {}
                if not isinstance(event_payload, Mapping) or event_payload.get("cli_session_sequence") in (None, ""):
                    continue
                events.append({"event_id": f"run:{item.id}", "source": "run", "run_id": str(item.run_id), "sequence": int(item.sequence or 0), "session_sequence": int(event_payload.get("cli_session_sequence") or 0), "event_type": item.event_type, "status": item.status, "message": item.message, "payload": event_payload, "created_at": _iso(item.created_at)})
            output_ids = {
                str((item.get("payload") or {}).get("assistant_message_id"))
                for item in events
                if item.get("event_type") == "cli.output"
                and isinstance(item.get("payload"), Mapping)
                and (item.get("payload") or {}).get("assistant_message_id")
            }
            valid_output_ids: list[uuid.UUID] = []
            for value in output_ids:
                try:
                    valid_output_ids.append(uuid.UUID(value))
                except (TypeError, ValueError):
                    continue
            output_rows: dict[str, ConversationMessage] = {}
            if valid_output_ids:
                output_rows = {
                    str(item.id): item
                    for item in (await db.execute(select(ConversationMessage).where(ConversationMessage.id.in_(valid_output_ids), ConversationMessage.session_id == row.id))).scalars().all()
                }
            for item in events:
                if item.get("event_type") != "cli.output":
                    continue
                payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
                message = output_rows.get(str(payload.get("assistant_message_id") or ""))
                if message is None:
                    continue
                text, actual_end, truncated = _bounded_text_chunk(str(message.content or ""), int(payload.get("start", 0) or 0), int(payload.get("end", 0) or 0))
                payload = dict(payload)
                payload["text"] = text
                payload["text_end"] = actual_end
                payload["text_truncated"] = truncated
                item["payload"] = payload
            events.sort(key=lambda item: (int(item.get("session_sequence") or 0), item.get("event_id") or ""))
            candidates = events[: limit + 1]
            has_more = len(candidates) > limit
            candidates = candidates[:limit]
            # Keep responses bounded even when a caller asks for 500 events.
            # A single output chunk is <=16KiB; never drop a pointer event from
            # an otherwise empty page.
            page: list[dict[str, Any]] = []
            page_bytes = 0
            for candidate in candidates:
                candidate_bytes = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
                if page and page_bytes + candidate_bytes > 64 * 1024:
                    has_more = True
                    break
                page.append(candidate)
                page_bytes += candidate_bytes
            next_cursor = None
            if page:
                final_sequence = max(int(item.get("session_sequence") or 0) for item in page)
                next_cursor = base64.urlsafe_b64encode(json.dumps({"version": 3, "session_id": sid, "sequence": final_sequence}, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
            elif token:
                # An exhausted cursor remains a valid stable position. This
                # prevents a client that polls an empty page from replaying
                # the entire session on the next request.
                next_cursor = str(token)
            return EventPage(tuple(page), next_cursor, has_more).to_dict()
        finally:
            await self._close(db)

    async def reconnect(self, session_id: Any, *, owner_id: str) -> dict[str, Any]:
        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        owner = _identifier(owner_id, field_name="owner_id")
        db = await self._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(sid))
            if row is None or row.deleted_at is not None:
                raise CLIDevelopmentSessionNotFound()
            if str(row.user_id) != owner:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            context = self._context(row)
            run, work = await self._load_latest(db, row.id)
            if self._is_remote_context(context):
                # Muse has no local native session handle or trusted Windows
                # filesystem.  Reconnect only rechecks the immutable Agent
                # binding, current authority and the durable remote binding
                # state; it never exposes a thread ID or credentials.
                status = self._status_from_rows(context, run, work)
                authority_valid = False
                remote_state = "unbound"
                remote_safe = False
                remote_context = (
                    row.context.get(MUSE_REMOTE_CONTEXT_KEY)
                    if isinstance(row.context, Mapping)
                    else None
                )
                remote_binding = (
                    remote_context.get("binding")
                    if isinstance(remote_context, Mapping)
                    and isinstance(remote_context.get("binding"), Mapping)
                    else remote_context
                )
                if isinstance(remote_binding, Mapping):
                    remote_state = str(remote_binding.get("state") or "unbound")[:32]
                    remote_safe = bool(
                        remote_binding.get("safe_to_send")
                        and not remote_binding.get("current_turn_nonce")
                        and remote_state not in {"unsafe", "thread_create_pending"}
                    )
                try:
                    self._assert_remote_admission(
                        owner_id=owner,
                        agent_id=str(context.get("agent_id") or row.agent_id or ""),
                    )
                    binding = await self._binding_for_row(db, row)
                    _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
                    await self._resolve_authority(
                        agent_id=str(context.get("agent_id") or row.agent_id or ""),
                        revision_id=str(context.get("agent_revision_id") or ""),
                        project_id=context.get("project_id"),
                        capabilities=REMOTE_SESSION_CAPABILITIES,
                    )
                    authority_valid = True
                except Exception:
                    authority_valid = False
                admission_ready = (
                    str(context.get("admission_state") or "") == "ready"
                    and bool(context.get("binding_id"))
                    and bool(context.get("binding_snapshot_hash"))
                )
                safe_to_send = bool(
                    authority_valid
                    and admission_ready
                    and status in {"ready", "completed"}
                    and (status == "ready" or remote_safe)
                )
                return {
                    "session_id": sid,
                    "status": status,
                    "native_handle": "remote_bound" if remote_state == "bound" else "remote_unbound",
                    "native_session_id": None,
                    "remote_binding_state": remote_state,
                    "safe_to_send": safe_to_send,
                    "run_id": str(run.id) if run else None,
                    "work_item_id": str(work.id) if work else None,
                    "reconciled": True,
                    **self._remote_result_fields(run, work),
                }
            native = "absent"
            native_id = None
            native_lease_free = False
            entries = (row.context or {}).get("cli_native_sessions") if isinstance(row.context, Mapping) else None
            if isinstance(entries, list):
                # The adapter stores the exact scope_key in CLISessionStore;
                # only a confirmed active entry is safe for a next send.
                expected = str(context.get("native_scope_key") or "")
                for entry in entries:
                    if isinstance(entry, Mapping) and (not expected or str(entry.get("scope_key") or "") == expected):
                        if str(entry.get("state") or "") == "active" and str(entry.get("native_session_id") or ""):
                            native = "confirmed"
                            native_id = "present"
                            native_lease_free = not entry.get("active_generation_id") and not entry.get("lease_expires_at")
                            break
            status = self._status_from_rows(context, run, work)
            if status in {"uncertain", "disconnected"}:
                status = "disconnected"
            authority_valid = False
            try:
                policy, current_cwd = self._policy_for_request(
                    owner_id=owner,
                    agent_id=str(context.get("agent_id") or row.agent_id or ""),
                    working_directory=str(context.get("working_directory") or ""),
                )
                permission_mode = self._validate_permission_mode(policy, str(context.get("permission_mode") or "native_default"))
                self._validate_provider_permission_mode(str(context.get("provider") or ""), permission_mode)
                expected_cache = self._provider_cache_root(policy, str(context.get("provider") or ""))
                stored_cache = str(context.get("provider_cache_root") or "")
                if os.path.normcase(os.path.realpath(stored_cache)) != os.path.normcase(os.path.realpath(expected_cache)):
                    raise CLIDevelopmentSessionUnavailable("provider state root changed", code="provider_state_root_changed")
                binding = await self._binding_for_row(db, row)
                _assert_route(binding, provider=str(context.get("provider") or ""), model=str(context.get("model") or ""), effort=str(context.get("effort") or ""))
                await self._resolve_authority(
                    agent_id=str(context.get("agent_id") or row.agent_id or ""),
                    revision_id=str(context.get("agent_revision_id") or ""),
                    project_id=context.get("project_id"),
                    capabilities=pinned_cli_capabilities(context),
                )
                authority_valid = bool(current_cwd)
            except Exception:
                authority_valid = False
            admission_ready = str(context.get("admission_state") or "") == "ready" and bool(context.get("binding_id")) and bool(context.get("binding_snapshot_hash"))
            safe_to_send = authority_valid and admission_ready and (
                status == "ready"
                or (
                    status in {"completed", "waiting_input", "waiting_approval", "cancelled"}
                    and native == "confirmed"
                    and str(context.get("native_state") or "") == "active"
                    and native_lease_free
                )
            )
            return {"session_id": sid, "status": status, "native_handle": native, "native_session_id": native_id, "safe_to_send": safe_to_send, "run_id": str(run.id) if run else None, "work_item_id": str(work.id) if work else None, "reconciled": True}
        finally:
            await self._close(db)

    async def record_native_scope(self, session_id: str, *, scope_key: str, state: str) -> None:
        """Store only the non-secret scope proof used by reconnect diagnostics."""

        db = await self._session()
        try:
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == uuid.UUID(str(session_id))).with_for_update())).scalars().first()
            if row is None:
                return
            context = self._context(row)
            context["native_scope_key"] = str(scope_key)[:128]
            context["native_state"] = str(state or "unknown")[:32]
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
            await db.commit()
        except Exception:
            await self._rollback_async(db)
            raise
        finally:
            await self._close(db)

    async def record_runtime_event(
        self,
        *,
        run_id: str,
        work_item_id: str | None = None,
        event_type: str,
        status: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one bounded provider lifecycle event to both ledgers."""

        db = await self._session()
        try:
            run = (await db.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(str(run_id))).with_for_update())).scalars().first()
            if run is None or str(run.run_type or "") not in {
                CLI_SESSION_SOURCE_TYPE,
                REMOTE_SESSION_SOURCE_TYPE,
            }:
                return
            session_row = (await db.execute(select(ConversationSession).where(ConversationSession.id == run.session_id).with_for_update())).scalars().first()
            if session_row is None:
                return
            session_sequence = self._allocate_event_sequences(session_row, 1)[0]
            run_events = (await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id).order_by(desc(AgentRunEvent.sequence)).limit(1))).scalars().first()
            next_run_sequence = int(getattr(run_events, "sequence", 0) or 0) + 1
            event_payload = dict(_safe_json(payload or {}) or {})
            event_payload["cli_session_sequence"] = session_sequence
            db.add(AgentRunEvent(run_id=run.id, sequence=next_run_sequence, event_type=str(event_type)[:80], status=str(status)[:32] if status else None, message=None, payload=event_payload, created_at=self._clock()))
            if work_item_id:
                work = (await db.execute(select(AgentWorkItem).where(AgentWorkItem.id == uuid.UUID(str(work_item_id))).with_for_update())).scalars().first()
                if work is not None and (
                    (
                        str(work.source_type or "") == CLI_SESSION_SOURCE_TYPE
                        and str(work.execution_adapter or "") == CLI_SESSION_ADAPTER_KEY
                    )
                    or (
                        str(work.source_type or "") == REMOTE_SESSION_SOURCE_TYPE
                        and str(work.execution_adapter or "") == MUSE_REMOTE_ADAPTER_KEY
                    )
                ):
                    work_events = (await db.execute(select(AgentWorkEvent).where(AgentWorkEvent.work_item_id == work.id).order_by(desc(AgentWorkEvent.sequence)).limit(1))).scalars().first()
                    next_work_sequence = int(getattr(work_events, "sequence", 0) or 0) + 1
                    allowed_work_statuses = {"pending", "claimed", "running", "awaiting_approval", "blocked", "retry_wait", "uncertain", "succeeded", "failed", "cancelled", "dead_letter", "transient", "permanent"}
                    work_status = str(status)[:32] if status and str(status)[:32] in allowed_work_statuses else None
                    db.add(AgentWorkEvent(work_item_id=work.id, sequence=next_work_sequence, event_type=str(event_type)[:80], from_state=str(work.state or "")[:32] or None, to_state=str(work.state or "")[:32] or None, status=work_status, actor_kind="service", actor_service_key="aoitalk.system", agent_run_id=run.id, payload_json=event_payload, created_at=self._clock()))
            await db.commit()
        except Exception:
            await self._rollback_async(db)
            raise
        finally:
            await self._close(db)

    async def record_execution_result(
        self,
        *,
        session_id: str,
        run_id: str,
        work_item_id: str,
        classification: str,
        output: str | None = None,
        assistant_text: str | None = None,
        assistant_message_id: str | None = None,
        assistant_text_truncated: bool = False,
        assistant_text_original_length: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        native_state: str | None = None,
        native_scope_key: str | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Persist provider evidence after the backend worker has drained.

        The adapter calls this only after ``execute_prompt`` returned (or its
        cancellation cleanup completed).  A coordinator may later settle the
        Work/Run fence; this method never upgrades an already-cancelled Run.
        """

        db = await self._session()
        try:
            sid = uuid.UUID(str(session_id))
            rid = uuid.UUID(str(run_id))
            wid = uuid.UUID(str(work_item_id))
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == sid).with_for_update())).scalars().first()
            run = (await db.execute(select(AgentRun).where(AgentRun.id == rid, AgentRun.session_id == sid).with_for_update())).scalars().first()
            work = (await db.execute(select(AgentWorkItem).where(AgentWorkItem.id == wid, AgentWorkItem.conversation_session_id == sid).with_for_update())).scalars().first()
            if row is None or run is None or work is None:
                raise CLIDevelopmentSessionNotFound()
            context = self._context(row)
            remote = self._is_remote_context(context)
            expected_source = REMOTE_SESSION_SOURCE_TYPE if remote else CLI_SESSION_SOURCE_TYPE
            expected_adapter = MUSE_REMOTE_ADAPTER_KEY if remote else CLI_SESSION_ADAPTER_KEY
            run_source_type = getattr(run, "run_type", None)
            work_source_type = getattr(work, "source_type", None)
            work_adapter = getattr(work, "execution_adapter", None)
            if (
                (
                    remote
                    and (
                        str(run_source_type or "") != expected_source
                        or str(work_source_type or "") != expected_source
                        or str(work_adapter or "") != expected_adapter
                    )
                )
                or (
                    not remote
                    and (
                        run_source_type not in (None, "", expected_source)
                        or work_source_type not in (None, "", expected_source)
                        or work_adapter not in (None, "", expected_adapter)
                    )
                )
            ):
                await db.rollback()
                return {
                    "discarded": True,
                    "status": "interrupted",
                    "run_id": str(rid),
                    "work_item_id": str(wid),
                    "reason": "binding_mismatch",
                }
            # A cancelled/settled Work owns the terminal fence.  Provider
            # threads may finish after an async cancellation, but their late
            # plaintext must never become a new assistant message.
            if (
                str(work.active_agent_run_id) != str(run.id)
                or str(run.work_item_id) != str(work.id)
                or str(run.status or "") in {"succeeded", "failed", "cancelled"}
                or str(work.state or "") in {"succeeded", "failed", "cancelled", "dead_letter", "uncertain"}
            ):
                await db.rollback()
                return {"discarded": True, "status": "interrupted", "run_id": str(rid), "work_item_id": str(wid)}
            if lease_owner is not None or lease_token is not None or attempt is not None:
                now = self._clock()
                if (
                    str(work.state or "") != "running"
                    or str(work.lease_owner or "") != str(lease_owner or "")
                    or str(work.lease_token or "") != str(lease_token or "")
                    or work.lease_expires_at is None
                    or work.lease_expires_at <= now
                    or int(work.attempt_count or 0) != int(attempt or 0)
                    or int(run.work_item_attempt or 0) != int(attempt or 0)
                ):
                    await db.rollback()
                    return {"discarded": True, "status": "interrupted", "run_id": str(rid), "work_item_id": str(wid), "reason": "lease_fence_lost"}
            normalized = str(classification or "failed").strip().lower()
            if normalized in {"success", "ok", "succeeded"}:
                normalized = "succeeded"
            elif normalized in {"awaiting_approval", "waiting_approval"}:
                normalized = "awaiting_approval"
            elif normalized in {"waiting_input", "input_required"}:
                normalized = "waiting_input"
            elif normalized in {"cancelled", "canceled"}:
                normalized = "cancelled"
            elif normalized in {"uncertain", "disconnected"}:
                normalized = "uncertain"
            else:
                normalized = "failed"
            output_content = ""
            output_message_id = None
            provider_message_id = None
            truncated_original_length = None
            if normalized == "succeeded":
                if remote:
                    # Only the provider adapter's confirmed-correlation
                    # fields are accepted.  Never infer a reply from an
                    # arbitrary outcome field or an uncertain history read.
                    provider_message_id = str(assistant_message_id or "").strip()
                    output_content = str(assistant_text or "")
                    try:
                        provided_original_length = (
                            None
                            if isinstance(assistant_text_original_length, bool)
                            else int(assistant_text_original_length)
                        )
                    except (TypeError, ValueError, OverflowError):
                        provided_original_length = None
                    truncated_original_length = (
                        max(len(output_content), provided_original_length)
                        if assistant_text_truncated
                        and provided_original_length is not None
                        else None
                    )
                    if (
                        not provider_message_id
                        or len(provider_message_id) > 512
                        or any(ord(char) < 0x20 for char in provider_message_id)
                        or not output_content
                        or (assistant_text_truncated and truncated_original_length is None)
                    ):
                        await db.rollback()
                        return {
                            "discarded": True,
                            "status": "interrupted",
                            "run_id": str(rid),
                            "work_item_id": str(wid),
                            "reason": "remote_reply_unconfirmed",
                        }
                # The durable assistant message is the only transcript copy;
                # Run/Work metadata carries its identity and no raw output.
                assistant_key = (
                    f"muse-assistant:{rid}"
                    if remote
                    else f"cli-assistant:{rid}"
                )
                message = (await db.execute(select(ConversationMessage).where(ConversationMessage.session_id == sid, ConversationMessage.client_message_id == assistant_key).limit(1))).scalars().first()
                if message is not None and remote:
                    existing_provider_id = str(
                        (message.message_metadata or {}).get("provider_message_id") or ""
                    )
                    if (
                        existing_provider_id != provider_message_id
                        or str(message.content or "") != output_content
                    ):
                        await db.rollback()
                        return {
                            "discarded": True,
                            "status": "interrupted",
                            "run_id": str(rid),
                            "work_item_id": str(wid),
                            "reason": "remote_reply_conflict",
                        }
                if message is None:
                    message_metadata = {
                        "cli_development": True,
                        "run_id": str(rid),
                    }
                    if remote:
                        # Provider identity is useful for replay correlation;
                        # credentials and managed role text never enter this
                        # metadata.
                        message_metadata.update(
                            {
                                "execution_target": MUSE_EXECUTION_TARGET,
                                "provider_message_id": provider_message_id,
                            }
                        )
                        if assistant_text_truncated:
                            message_metadata["assistant_text_truncated"] = True
                            message_metadata[
                                "assistant_text_original_length"
                            ] = truncated_original_length
                    message = ConversationMessage(id=uuid.uuid4(), session_id=sid, role="assistant", content=(output_content if remote else str(output or "")), client_message_id=assistant_key, sender_type="agent", sender_id=str(run.agent_id), message_metadata=message_metadata, created_at=self._clock(), updated_at=self._clock(), is_active_branch=True, branch_index=0)
                    db.add(message)
                    row.message_count = int(row.message_count or 0) + 1
                # New messages are still pending when autoflush is disabled.
                # Use this exact message for both persistence and output ranges.
                output_content = str(message.content or "")
                output_message_id = str(message.id)
                run.result = {
                    "assistant_message_id": output_message_id,
                    **(
                        {"provider_assistant_message_id": provider_message_id}
                        if remote and provider_message_id
                        else {}
                    ),
                    **(
                        {
                            "assistant_text_truncated": True,
                            "assistant_text_original_length": truncated_original_length,
                        }
                        if remote and assistant_text_truncated
                        else {}
                    ),
                }
                run.validation = {"provider_output_persisted": True}
                work.result_summary = (
                    "Muse remote turn completed"
                    if remote
                    else "CLI session turn completed"
                )
                target_status = "completed"
            elif normalized == "awaiting_approval":
                target_status = "waiting_approval"
            elif normalized == "waiting_input":
                target_status = "waiting_input"
            elif normalized == "cancelled":
                target_status = "cancelled"
            elif normalized == "uncertain":
                target_status = "interrupted" if "interrupt" in str(error_code or "").lower() else "disconnected"
            else:
                target_status = "failed"
            if str(run.status or "") not in {"cancelled", "succeeded", "failed"}:
                if target_status == "completed":
                    run.status = "succeeded"
                elif target_status == "cancelled":
                    run.status = "cancelled"
                elif target_status == "waiting_approval":
                    run.status = "awaiting_approval"
                else:
                    run.status = "failed"
                run.error = str(error_message or error_code or "")[:4000] or None
                run.updated_at = self._clock()
                run.ended_at = self._clock() if target_status in CLI_DEVELOPMENT_TERMINAL_STATUSES else None
            context["status"] = target_status
            if native_state:
                context["native_state"] = str(native_state)[:32]
            if native_scope_key:
                context["native_scope_key"] = str(native_scope_key)[:128]
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
            row.development_status = "working" if target_status in {"queued", "running"} else "waiting_for_user"
            # Output remains encrypted in ConversationMessage.  Run events
            # carry only a bounded pointer/range so event collection can
            # decrypt under the session ACL without duplicating plaintext in
            # the ledger.
            event_tail = (await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id).order_by(desc(AgentRunEvent.sequence)).limit(1))).scalars().first()
            next_sequence = int(getattr(event_tail, "sequence", 0) or 0) + 1
            chunk_count = 0
            if output_message_id is not None:
                chunk_size = 4096
                chunk_count = max(1, (len(output_content) + chunk_size - 1) // chunk_size)
            feed_sequences = self._allocate_event_sequences(row, chunk_count + 1)
            output_sequence_index = 0
            if chunk_count:
                for chunk_index in range(chunk_count):
                    start = chunk_index * chunk_size
                    end = min(len(output_content), start + chunk_size)
                    chunk_payload = {
                        "assistant_message_id": output_message_id,
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "start": start,
                        "end": end,
                        "cli_session_sequence": feed_sequences[output_sequence_index],
                    }
                    if remote and provider_message_id:
                        chunk_payload["provider_assistant_message_id"] = provider_message_id
                    if remote and assistant_text_truncated:
                        chunk_payload["assistant_text_truncated"] = True
                        chunk_payload[
                            "assistant_text_original_length"
                        ] = truncated_original_length
                    db.add(AgentRunEvent(run_id=run.id, sequence=next_sequence, event_type="cli.output", status="completed", message=None, payload=chunk_payload, created_at=self._clock()))
                    next_sequence += 1
                    output_sequence_index += 1
            tail_payload = {
                "assistant_message_id": output_message_id,
                "error_code": str(error_code or "")[:96],
                "cli_session_sequence": feed_sequences[-1],
            }
            if remote and provider_message_id:
                tail_payload["provider_assistant_message_id"] = provider_message_id
            if remote and assistant_text_truncated and truncated_original_length is not None:
                tail_payload["assistant_text_truncated"] = True
                tail_payload[
                    "assistant_text_original_length"
                ] = truncated_original_length
            if error_message:
                tail_payload["error_message"] = str(error_message)[:1024]
            db.add(AgentRunEvent(run_id=run.id, sequence=next_sequence, event_type=f"cli.{target_status}", status=target_status, message=str(error_message)[:1024] if error_message else None, payload=tail_payload, created_at=self._clock()))
            await db.commit()
            receipt_dict: dict[str, Any] = {
                "status": target_status,
                "run_id": str(rid),
                "work_item_id": str(wid),
                "assistant_message_id": (
                    (run.result or {}).get("assistant_message_id")
                    if isinstance(run.result, Mapping)
                    else None
                ),
            }
            if remote and isinstance(run.result, Mapping):
                receipt_dict["provider_assistant_message_id"] = run.result.get(
                    "provider_assistant_message_id"
                )
                if run.result.get("assistant_text_truncated") is True:
                    receipt_dict["assistant_text_truncated"] = True
                    receipt_dict["assistant_text_original_length"] = run.result.get(
                        "assistant_text_original_length"
                    )
            if error_message:
                receipt_dict["error_message"] = str(error_message)[:1024]
            return receipt_dict
        except Exception:
            await self._rollback_async(db)
            raise
        finally:
            await self._close(db)

    async def cancel(self, session_id: Any, run_id: Any, *, owner_id: str, reason: str = "owner_cancelled") -> dict[str, Any]:
        sid = _safe_uuid(session_id)
        rid = _safe_uuid(run_id)
        if sid is None or rid is None:
            raise CLIDevelopmentSessionInvalid("invalid session or run id", code="invalid_identity")
        owner = _identifier(owner_id, field_name="owner_id")
        db = await self._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(sid))
            run = await db.get(AgentRun, uuid.UUID(rid))
            if row is None or getattr(row, "deleted_at", None) is not None or run is None or run.session_id != row.id:
                raise CLIDevelopmentSessionNotFound()
            if str(row.user_id) != owner or str(run.user_id) != owner:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            work = await db.get(AgentWorkItem, run.work_item_id) if run.work_item_id else None
            if work is None:
                raise CLIDevelopmentSessionConflict("CLI Work binding is missing", code="work_binding_missing")
            remote = self._is_remote_context(self._context(row))
            expected_source = REMOTE_SESSION_SOURCE_TYPE if remote else CLI_SESSION_SOURCE_TYPE
            expected_adapter = MUSE_REMOTE_ADAPTER_KEY if remote else CLI_SESSION_ADAPTER_KEY
            if str(run.run_type or "") != expected_source or str(work.source_type or "") != expected_source or str(work.execution_adapter or "") != expected_adapter:
                raise CLIDevelopmentSessionConflict("CLI Work binding is missing", code="work_binding_missing")
            current_status = self._status_from_rows(self._context(row), run, work)
            if current_status in CLI_DEVELOPMENT_TERMINAL_STATUSES or str(work.state or "") in {"succeeded", "failed", "cancelled", "dead_letter", "uncertain"}:
                return {"accepted": False, "session_id": sid, "run_id": rid, "work_item_id": str(work.id), "cancel_requested": False, "status": current_status, "reason": "already_terminal", "result": False}
            await self._close(db)
            db = None
            if self.coordinator is None or not callable(getattr(self.coordinator, "cancel", None)):
                raise CLIDevelopmentSessionUnavailable("shared Work coordinator is unavailable", code="work_coordinator_unavailable")
            result = await self.coordinator.cancel(str(work.id), actor={"id": owner, "actor_type": "human"}, reason=reason)
            try:
                from ..llm.generation_cancellation import request_generation_cancellation

                request_generation_cancellation(rid)
            except Exception:
                pass
            accepted = bool(result)
            if accepted and remote:
                # The shared coordinator owns the durable Work cancellation;
                # the provider adapter only receives a local stop signal and
                # must never claim that the remote side was cancelled.
                try:
                    remote_adapter = self._get_remote_adapter()
                    cancel_remote = getattr(remote_adapter, "cancel", None)
                    if callable(cancel_remote):
                        value = cancel_remote(str(work.id))
                        if inspect.isawaitable(value):
                            await value
                except Exception:
                    pass
            if accepted and (remote or str(run.status or "") == "queued"):
                try:
                    from .agent_run_service import AgentRunService

                    await AgentRunService(self._db_manager, config=self.config).cancel_run(rid, message=reason)
                except Exception:
                    pass
            if accepted and remote:
                try:
                    await self.record_runtime_event(
                        run_id=rid,
                        work_item_id=str(work.id),
                        event_type="cli.cancelled",
                        status="cancelled",
                        payload={"reason": str(reason or "cancelled")[:96]},
                    )
                except Exception:
                    pass
            fresh = await self.get_session(sid, owner_id=owner)
            remote_fields = (
                {
                    key: fresh.get(key)
                    for key in (
                        "execution_target",
                        "local_filesystem",
                        "sandbox",
                        "turn_status",
                        "delivery",
                    )
                }
                if remote
                else {}
            )
            return {"accepted": accepted, "session_id": sid, "run_id": rid, "work_item_id": str(work.id), "cancel_requested": accepted, "status": fresh.get("status"), "session": fresh, "result": result, **remote_fields}
        finally:
            if db is not None:
                await self._close(db)

    async def finalize_cancelled_run(self, *, session_id: str, run_id: str, work_item_id: str, reason: str = "cancelled", native_state: str = "cancelled") -> bool:
        """Close the Run after the provider worker has fully drained."""

        db = await self._session()
        try:
            sid = uuid.UUID(str(session_id))
            rid = uuid.UUID(str(run_id))
            wid = uuid.UUID(str(work_item_id))
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == sid).with_for_update())).scalars().first()
            run = (await db.execute(select(AgentRun).where(AgentRun.id == rid, AgentRun.session_id == sid).with_for_update())).scalars().first()
            work = (await db.execute(select(AgentWorkItem).where(AgentWorkItem.id == wid, AgentWorkItem.conversation_session_id == sid).with_for_update())).scalars().first()
            expected_source = (
                REMOTE_SESSION_SOURCE_TYPE
                if row is not None and self._is_remote_context(self._context(row))
                else CLI_SESSION_SOURCE_TYPE
            )
            expected_adapter = (
                MUSE_REMOTE_ADAPTER_KEY
                if expected_source == REMOTE_SESSION_SOURCE_TYPE
                else CLI_SESSION_ADAPTER_KEY
            )
            if (
                row is None
                or run is None
                or work is None
                or str(work.source_type or "") != expected_source
                or str(work.execution_adapter or "") != expected_adapter
                or str(work.state or "") != "cancelled"
            ):
                await db.rollback()
                return False
            if str(run.status or "") in {"cancelled", "failed", "succeeded"}:
                await db.rollback()
                return str(run.status or "") == "cancelled"
            run.status = "cancelled"
            run.error = str(reason or "cancelled")[:4000]
            run.ended_at = self._clock()
            run.updated_at = self._clock()
            context = self._context(row)
            remote = self._is_remote_context(context)
            context["status"] = "cancelled"
            if remote:
                # A remote Muse thread has no local native-session handle.
                context.pop("native_state", None)
            else:
                context["native_state"] = str(native_state or "cancelled")[:32]
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
            row.development_status = "waiting_for_user"
            tail = (await db.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id).order_by(desc(AgentRunEvent.sequence)).limit(1))).scalars().first()
            run_sequence = int(getattr(tail, "sequence", 0) or 0) + 1
            session_sequence = self._allocate_event_sequences(row, 1)[0]
            event_payload = {
                "reason": str(reason or "cancelled")[:96],
                "cli_session_sequence": session_sequence,
            }
            if remote:
                event_payload.update(
                    {
                        "execution_target": MUSE_EXECUTION_TARGET,
                        "local_filesystem": False,
                        "sandbox": "remote_uncontrolled",
                        "turn_status": "local_stopped",
                        "remote_cancel_status": "remote_unconfirmed",
                    }
                )
            db.add(AgentRunEvent(run_id=run.id, sequence=run_sequence, event_type="cli.cancelled", status="cancelled", message=None, payload=event_payload, created_at=self._clock()))
            await db.commit()
            return True
        except Exception:
            await self._rollback_async(db)
            return False
        finally:
            await self._close(db)

    async def list_sessions(self, *, owner_id: str, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        owner = _identifier(owner_id, field_name="owner_id")
        limit = max(1, min(int(limit or 100), 500))
        db = await self._session()
        try:
            cli_context = ConversationSession.context[CLI_DEVELOPMENT_CONTEXT_KEY].as_string().is_not(None)
            policy = cli_development_policy(self.config)
            statement = select(ConversationSession).where(ConversationSession.user_id == owner, ConversationSession.deleted_at.is_(None), cli_context).order_by(desc(ConversationSession.last_activity), desc(ConversationSession.id))
            selected: list[dict[str, Any]] = []
            offset = 0
            page_size = max(100, limit)
            while len(selected) < limit:
                rows = (await db.execute(statement.offset(offset).limit(page_size))).scalars().all()
                if not rows:
                    break
                for row in rows:
                    if not self._context(row):
                        continue
                    projection = await self._session_projection(db, row, policy=policy, owner_id=owner, created=False)
                    if status is None or str(projection.get("status") or "") == str(status):
                        selected.append(projection)
                        if len(selected) >= limit:
                            break
                if len(rows) < page_size:
                    break
                offset += page_size
            return selected
        finally:
            await self._close(db)

    def bind_coordinator(self, coordinator: Any, *, backend_factory: Any | None = None, session_store: Any | None = None) -> Any:
        """Register this service's adapter on the caller-owned coordinator."""

        from .cli_session_work_adapter import (
            register_cli_session_work,
            register_muse_remote_session_work,
        )

        self.coordinator = coordinator
        self.session_store = session_store or self.session_store
        adapter = register_cli_session_work(
            coordinator,
            self,
            backend_factory=backend_factory,
            session_store=self.session_store,
        )
        register_muse_remote_session_work(
            coordinator,
            self,
            remote_adapter=self._get_remote_adapter(),
        )
        return adapter

__all__ = [
    "CLI_SESSION_SOURCE_TYPE",
    "CLI_SESSION_ADAPTER_KEY",
    "CLI_DEVELOPMENT_CONTEXT_KEY",
    "CLI_DEVELOPMENT_CAPABILITIES",
    "normalize_cli_capabilities",
    "pinned_cli_capabilities",
    "CLIDevelopmentPolicy",
    "CreateCLIDevelopmentSessionRequest",
    "SendCLIDevelopmentSessionRequest",
    "EventPage",
    "MuseRemoteTurnWorkSource",
    "CLIDevelopmentSessionError",
    "CLIDevelopmentSessionNotFound",
    "CLIDevelopmentSessionForbidden",
    "CLIDevelopmentSessionConflict",
    "CLIDevelopmentSessionInvalid",
    "CLIDevelopmentSessionUnavailable",
    "cli_development_policy",
    "canonical_windows_cwd",
    "CLIDevelopmentSessionService",
    "REMOTE_SESSION_SOURCE_TYPE",
    "REMOTE_SESSION_CAPABILITIES",
]
