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

import asyncio
import base64
import copy
import hashlib
import importlib
import inspect
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

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
    MAX_MUSE_OUTPUT_CHARS,
    REMOTE_AGENT_DELEGATE,
    MuseErrorCode,
)
from .muse_cli_diagnostics import (
    sanitize_cause,
    sanitize_cli_failure_diagnostic,
)
from .muse_remote_session_store import (
    MUSE_REMOTE_CONTEXT_KEY,
    MuseRemotePrivacyGateway,
    MuseRemoteSessionStore,
    MuseRemoteTargetResolver,
)


def _managed_muse_cli_executable(
    *, platform: str, environ: Mapping[str, str], home: Path
) -> Path:
    """Resolve the pinned CLI without creating or installing anything."""
    if platform == "win32":
        root_value = environ.get("LOCALAPPDATA", "")
        executable_parts = ("Scripts", "muse-cli.exe")
    elif platform == "darwin":
        root_value = str(home / "Library" / "Application Support")
        executable_parts = ("bin", "muse-cli")
    else:
        root_value = environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))
        executable_parts = ("bin", "muse-cli")
    root = Path(root_value)
    if not root_value or not root.is_absolute():
        raise ValueError("Muse CLI user data root must be an absolute path")
    root = root.resolve()
    executable = (
        root / "AoiTalk" / "runtimes" / "muse-cli" / "0.3.2" / "venv"
    ).joinpath(*executable_parts).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    if root.is_relative_to(repository_root) or executable.is_relative_to(repository_root):
        raise ValueError("Muse CLI user data root must be outside the repository")
    return executable


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
_REMOTE_HISTORY_RECONCILIATION_LOCKS: dict[str, asyncio.Lock] = {}
_REMOTE_HISTORY_RECONCILIATION_LOCKS_GUARD = asyncio.Lock()
_MUSE_CLI_PROCESS_FAILED = "cli_process_failed"
_SHARED_CLI_DEVELOPMENT_SERVICES: dict[int, tuple[Any, Any]] = {}


def get_shared_cli_development_session_service(
    db_manager: Any | None = None,
    *,
    config: Any | None = None,
) -> Any | None:
    """Return the server-composed CLI session service for one DB manager.

    Delegate workers must join the already registered AgentWork coordinator;
    constructing an unbound service here would persist work that nobody can
    execute.  Tests and embedded callers can inject the service explicitly.
    """

    manager = db_manager
    if manager is None:
        from ..memory.database import get_database_manager

        manager = get_database_manager()
    entry = _SHARED_CLI_DEVELOPMENT_SERVICES.get(id(manager))
    if entry is None:
        return None
    registered_manager, service = entry
    if registered_manager is not manager or getattr(service, "coordinator", None) is None:
        return None
    if config is not None and getattr(service, "config", None) is not config:
        return None
    return service


def _register_shared_cli_development_session_service(service: Any) -> None:
    manager = getattr(service, "_db_manager", None)
    if manager is None or getattr(service, "coordinator", None) is None:
        return
    _SHARED_CLI_DEVELOPMENT_SERVICES[id(manager)] = (manager, service)


async def _remote_history_reconciliation_lock(session_id: str) -> asyncio.Lock:
    """Serialize same-process reconnect recovery for one remote session."""

    async with _REMOTE_HISTORY_RECONCILIATION_LOCKS_GUARD:
        lock = _REMOTE_HISTORY_RECONCILIATION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _REMOTE_HISTORY_RECONCILIATION_LOCKS[session_id] = lock
        return lock


def _safe_remote_delivery(value: Any) -> dict[str, Any]:
    """Keep only the bounded role-delivery receipt emitted by Muse."""

    source = value if isinstance(value, Mapping) else {}
    mode = "managed_role" if source.get("mode") == "managed_role" else "raw"
    receipt: dict[str, Any] = {
        "mode": mode,
        "mechanism": "first_turn_user_prefix" if mode == "managed_role" else "none",
        "system_prompt_applied": False,
        "native_persona_replaced": False,
        "adherence_guaranteed": False,
    }
    if mode == "managed_role":
        revision_id = source.get("role_revision_id")
        digest = source.get("role_context_sha256")
        if isinstance(revision_id, str) and len(revision_id) <= 256:
            receipt["role_revision_id"] = revision_id
        if (
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest.lower())
        ):
            receipt["role_context_sha256"] = digest.lower()
        receipt["limitation"] = (
            "Muse CLI has no system prompt flag; role text was sent in the first user turn."
        )
    return receipt


def _muse_curl56_cause_matches(value: Any) -> bool:
    """Match the closed Muse diagnostic for the known ambiguous send failure."""

    return bool(
        isinstance(value, Mapping)
        and value.get("cause_code") == _MUSE_CLI_PROCESS_FAILED
        and value.get("command") == "send"
        and value.get("stage") == "api_call"
        and value.get("phase_class") == "handshake"
        and value.get("exception_type") == "curl_cffi.curl.CurlError"
        and type(value.get("curl_code")) is int
        and value.get("curl_code") == 56
        and type(value.get("returncode")) is int
        and value.get("returncode") == 1
        and value.get("timed_out") is False
    )


def _muse_curl56_recovery_evidence_matches(
    binding: Any, result_summary: Any
) -> bool:
    """Require both persisted binding and Work evidence for history-only recovery."""

    return bool(
        isinstance(binding, Mapping)
        and binding.get("state") == "unsafe"
        and binding.get("safe_to_send") is False
        and binding.get("unsafe_reason") == _MUSE_CLI_PROCESS_FAILED
        and _muse_curl56_cause_matches(binding.get("unsafe_cause"))
        and isinstance(result_summary, Mapping)
        and result_summary.get("execution_target") == MUSE_EXECUTION_TARGET
        and result_summary.get("local_filesystem") is False
        and result_summary.get("sandbox") == "remote_uncontrolled"
        and result_summary.get("turn_status") == "uncertain"
        and result_summary.get("send_attempted") is True
        and result_summary.get("send_attempted_known") is True
        and result_summary.get("acceptance") == "acceptance_unconfirmed"
        and result_summary.get("safe_to_send") is False
        and _muse_curl56_cause_matches(result_summary.get("cause"))
    )


def _remote_recovery_error_matches(
    *, run_error: Any, work_error: Any, binding: Any, result_summary: Any
) -> bool:
    """Keep legacy recovery while admitting one fully evidenced CLI failure."""

    run_code = str(run_error or "")
    work_code = str(work_error or "")
    if run_code != work_code:
        return False
    if run_code == str(MuseErrorCode.SEND_RESPONSE_UNCORRELATED):
        return True
    return run_code == _MUSE_CLI_PROCESS_FAILED and _muse_curl56_recovery_evidence_matches(
        binding, result_summary
    )


def _matching_muse_tool_loop_opt_in(
    work_metadata: Any, run_cli_metadata: Any
) -> bool | None:
    """Return the matching persisted opt-in, or None for malformed/mismatched records."""

    work_value = (
        work_metadata.get("muse_tool_loop_opt_in", False)
        if isinstance(work_metadata, Mapping)
        else False
    )
    run_value = (
        run_cli_metadata.get("muse_tool_loop_opt_in", False)
        if isinstance(run_cli_metadata, Mapping)
        else False
    )
    if type(work_value) is not bool or type(run_value) is not bool:
        return None
    if work_value != run_value:
        return None
    return work_value


def _remote_uncertain_event_matches(
    event: Any, *, run_id: Any, attempt: int, error_code: str
) -> bool:
    """Match the coordinator's append-only uncertain event without status."""

    if (
        event is None
        or str(getattr(event, "event_type", "") or "") != "work.uncertain"
        or str(getattr(event, "to_state", "") or "") != "uncertain"
        or getattr(event, "status", None) not in (None, "uncertain")
        or error_code
        not in {
            str(MuseErrorCode.SEND_RESPONSE_UNCORRELATED),
            _MUSE_CLI_PROCESS_FAILED,
        }
        or str(getattr(event, "safe_error_code", "") or "") != error_code
        or str(getattr(event, "agent_run_id", "") or "") != str(run_id)
    ):
        return False
    payload = getattr(event, "payload_json", None)
    if not isinstance(payload, Mapping):
        payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        payload = {}
    classification = payload.get("classification")
    if (
        classification not in (None, "uncertain")
        or (
            error_code == _MUSE_CLI_PROCESS_FAILED
            and classification != "uncertain"
        )
        or (
            error_code == _MUSE_CLI_PROCESS_FAILED
            and payload.get("error_code") != error_code
        )
    ):
        return False
    event_attempt = payload.get("attempt")
    if error_code == _MUSE_CLI_PROCESS_FAILED and (
        type(event_attempt) is not int or event_attempt != attempt
    ):
        return False
    if event_attempt is not None:
        if isinstance(event_attempt, bool):
            return False
        try:
            if int(event_attempt) != int(attempt):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True
CLI_PROVIDER_PERMISSION_MODES = {
    "antigravity-cli": frozenset({"native_default", "standard", "accept_edits"}),
    "claude-cli": frozenset({"native_default", "standard", "accept_edits"}),
    "codex-cli": frozenset({"native_default", "standard"}),
    "grok-cli": frozenset({"native_default", "standard"}),
}
REMOTE_SESSION_CAPABILITIES = (REMOTE_AGENT_DELEGATE,)


# Bounded, owner-scoped observation of the latest remote reconnect attempt.
# The receipt is diagnostics only: it never feeds safe_to_send, the Muse
# binding, the provider turn nonce, Run/Work state or any retry decision.
REMOTE_RECONNECT_RECEIPT_KEY = "remote_reconnect_receipt"
REMOTE_RECONNECT_SEQUENCE_KEY = "remote_reconnect_attempt_sequence"
REMOTE_RECONNECT_RECEIPT_SCHEMA_VERSION = 1
REMOTE_RECONNECT_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_REMOTE_RECONNECT_DIAGNOSTICS = frozenset(
    {
        "confirmed",
        "history_unavailable",
        "history_nonterminal",
        "history_unconfirmed",
        "binding_mismatch",
        "authority_unavailable",
        "binding_persist_failed",
        "recovery_state_ineligible",
        "no_pending_nonce",
        "settlement_conflict",
        "settlement_unavailable",
        "tool_loop_metadata_mismatch",
        "tool_loop_recovery_required",
        "history_observation_conflict",
    }
)
_REMOTE_RECONNECT_RECEIPT_FIELDS = (
    "schema_version",
    "attempt_id",
    "attempt_sequence",
    "state",
    "terminal",
    "started_at",
    "finished_at",
    "history_outcome",
    "reconciled",
    "history_reconciled",
    "history_observed",
    "history_observation_present",
    "reconciliation_diagnostic",
    "session_status",
    "remote_binding_state",
    "run_id",
    "work_item_id",
    "error_type",
    "error_code",
)
_RECEIPT_TOKEN = re.compile(r"[a-z0-9_]{1,32}")
_RECEIPT_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_RECEIPT_ERROR_CODE = re.compile(r"[a-z0-9_.-]{1,64}")


def _receipt_token(value: Any, allowed: Iterable[str] | None = None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if allowed is not None:
        return value if value in allowed else "other"
    return value if _RECEIPT_TOKEN.fullmatch(value) else "other"


def _receipt_error_fields(error: BaseException | None) -> dict[str, Any]:
    """Expose only the exception class name and a typed code, never its text."""

    if error is None:
        return {"error_type": None, "error_code": None}
    name = type(error).__name__
    code = getattr(error, "code", None)
    return {
        "error_type": name if _RECEIPT_ERROR_TYPE.fullmatch(name) else "other",
        "error_code": (
            code
            if isinstance(code, str) and _RECEIPT_ERROR_CODE.fullmatch(code)
            else None
        ),
    }


def _remote_reconnect_receipt_projection(context: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = context.get(REMOTE_RECONNECT_RECEIPT_KEY)
    if not isinstance(raw, Mapping):
        return None
    return {key: copy.deepcopy(raw.get(key)) for key in _REMOTE_RECONNECT_RECEIPT_FIELDS}


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


_DELEGATE_WORKER_PROVIDERS = {
    "codex": "codex-cli",
    "antigravity": "antigravity-cli",
    "claude": "claude-cli",
    "grok": "grok-cli",
    "muse": MUSE_PROVIDER,
}
_DELEGATE_WORKER_BUDGET_LIMITS = {
    "max_tokens": 1_000_000,
    "max_cost_micros": 1_000_000_000_000,
    "max_attempts": 100,
}


def _typed_delegate_worker_context(value: Any) -> dict[str, Any] | None:
    """Accept only the internal runtime marker, never a caller mapping."""

    if value is None:
        return None
    from .delegate_worker_runtime import DelegateWorkerSessionContext

    if not isinstance(value, DelegateWorkerSessionContext):
        raise CLIDevelopmentSessionInvalid(
            "managed worker context must be an internal marker",
            code="delegate_worker_context_invalid",
        )
    try:
        marker = copy.deepcopy(dict(value.to_dict()))
    except Exception as exc:
        raise CLIDevelopmentSessionInvalid(
            "managed worker context is invalid",
            code="delegate_worker_context_invalid",
        ) from exc
    return marker


def _validate_delegate_worker_context(
    marker: Mapping[str, Any] | None,
    request: CreateCLIDevelopmentSessionRequest,
    *,
    agent_id: str,
    revision_id: str,
) -> dict[str, Any] | None:
    """Validate an internal worker marker against the fixed session route."""

    if marker is None:
        return None
    normalized = copy.deepcopy(dict(marker))
    expected_marker_keys = {
        "schema_version",
        "dialogue_id",
        "kind",
        "workspace_scope",
        "workspace_scope_sha256",
        "timeout_seconds",
        "budgets",
        "owner_id",
        "agent_id",
        "agent_revision_id",
        "agent_dialogue_tool_ids",
        "allow_cli_native_tools",
        "capabilities",
    }
    kind = str(normalized.get("kind") or "").strip().casefold()
    scope = normalized.get("workspace_scope")
    if (
        set(normalized) != expected_marker_keys
        or type(normalized.get("schema_version")) is not int
        or normalized.get("schema_version") != 1
        or _safe_uuid(normalized.get("dialogue_id")) is None
        or kind not in _DELEGATE_WORKER_PROVIDERS
        or str(normalized.get("owner_id") or "") != request.owner_id
        or str(normalized.get("agent_id") or "") != agent_id
        or str(normalized.get("agent_revision_id") or "") != revision_id
        or request.provider != _DELEGATE_WORKER_PROVIDERS[kind]
        or not isinstance(scope, Mapping)
        or set(scope) != {"mode", "root", "paths"}
        or scope.get("mode") not in {"read", "write"}
        or not isinstance(scope.get("root"), str)
        or len(scope.get("root") or "") > 512
        or not isinstance(scope.get("paths"), list)
        or not scope["paths"]
        or len(scope["paths"]) > 64
        or normalized.get("workspace_scope_sha256") != _json_hash(dict(scope))
    ):
        raise CLIDevelopmentSessionConflict(
            "managed worker context differs from the fixed session route",
            code="delegate_worker_context_mismatch",
        )
    try:
        raw_root = Path(scope["root"]).expanduser()
        if not raw_root.is_absolute():
            raise ValueError
        root = Path(scope["root"]).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        if str(root).casefold() != str(scope["root"]).casefold():
            raise ValueError
        canonical_paths: list[Path] = []
        for raw_path in scope["paths"]:
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError
            supplied_path = Path(raw_path).expanduser()
            if not supplied_path.is_absolute():
                raise ValueError
            path = supplied_path.resolve(strict=True)
            if not path.is_dir():
                raise ValueError
            try:
                path.relative_to(root)
            except ValueError:
                raise ValueError from None
            canonical_paths.append(path)
        if any(str(path).casefold() != str(raw).casefold() for path, raw in zip(canonical_paths, scope["paths"])):
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise CLIDevelopmentSessionConflict(
            "managed worker scope is unavailable or outside its root",
            code="delegate_worker_scope_invalid",
        ) from None
    timeout = normalized.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 3600
    ):
        raise CLIDevelopmentSessionInvalid(
            "managed worker timeout is invalid",
            code="delegate_worker_timeout_invalid",
        )
    budgets = normalized.get("budgets")
    if not isinstance(budgets, Mapping) or any(
        key not in _DELEGATE_WORKER_BUDGET_LIMITS
        or isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _DELEGATE_WORKER_BUDGET_LIMITS[key]
        for key, value in budgets.items()
    ):
        raise CLIDevelopmentSessionInvalid(
            "managed worker budgets are invalid",
            code="delegate_worker_budget_invalid",
        )
    if any(
        not isinstance(path, str) or not path or len(path) > 512
        for path in scope["paths"]
    ):
        raise CLIDevelopmentSessionInvalid(
            "managed worker scope paths are invalid",
            code="delegate_worker_scope_invalid",
        )
    try:
        expected_caps = (
            REMOTE_SESSION_CAPABILITIES
            if kind == "muse"
            else normalize_cli_capabilities(request.allowed_capabilities)
        )
        marker_caps = normalize_cli_capabilities(normalized.get("capabilities")) if kind != "muse" else tuple(normalized.get("capabilities") or ())
    except CLIDevelopmentSessionInvalid:
        raise
    if (
        not isinstance(normalized.get("capabilities"), list)
        or tuple(sorted(marker_caps)) != tuple(sorted(expected_caps))
        or normalized.get("allow_cli_native_tools") is not (kind != "muse")
        or normalized.get("agent_dialogue_tool_ids") != []
    ):
        raise CLIDevelopmentSessionConflict(
            "managed worker capabilities are invalid",
            code="delegate_worker_capability_mismatch",
        )
    if kind != "muse":
        requested_cwd = request.working_directory
        if not requested_cwd or str(Path(requested_cwd).expanduser().resolve()).casefold() != str(canonical_paths[0]).casefold():
            raise CLIDevelopmentSessionConflict(
                "managed worker working directory differs from its scope",
                code="delegate_worker_scope_mismatch",
            )
    return normalized


def _assert_delegate_worker_receipt(
    context: Mapping[str, Any], marker: Mapping[str, Any] | None
) -> None:
    stored = context.get("delegate_worker")
    if marker is None and stored is None:
        return
    if not isinstance(stored, Mapping) or marker is None or dict(stored) != dict(marker):
        raise CLIDevelopmentSessionConflict(
            "session idempotency key has a different managed worker scope",
            code="delegate_worker_idempotency_conflict",
        )


def _delegate_worker_audit_marker(marker: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Copy only bounded scope proof and limits into Work/Run metadata."""

    if marker is None:
        return None
    return {
        key: copy.deepcopy(marker[key])
        for key in (
            "schema_version",
            "dialogue_id",
            "kind",
            "workspace_scope_sha256",
            "timeout_seconds",
            "budgets",
            "allow_cli_native_tools",
            "capabilities",
            "agent_dialogue_tool_ids",
        )
        if key in marker
    }


def _safe_delegate_worker_usage(value: Any) -> dict[str, int] | None:
    """Keep only bounded numeric provider usage counters."""

    if not isinstance(value, Mapping):
        return None
    allowed = {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cost_micros",
        "estimated_cost_micros",
    }
    projected: dict[str, int] = {}
    for key, item in list(value.items())[:32]:
        normalized_key = str(key).strip().casefold()
        if (
            normalized_key in allowed
            and isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 10**15
        ):
            projected[normalized_key] = item
    return projected or None


def _typed_delegate_worker_limits(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    from .delegate_worker_runtime import DelegateWorkerRunLimits

    if not isinstance(value, DelegateWorkerRunLimits):
        raise CLIDevelopmentSessionInvalid(
            "managed worker limits must be an internal marker",
            code="delegate_worker_limits_invalid",
        )
    try:
        return copy.deepcopy(dict(value.to_dict()))
    except Exception as exc:
        raise CLIDevelopmentSessionInvalid(
            "managed worker limits are invalid",
            code="delegate_worker_limits_invalid",
        ) from exc


def _validate_delegate_worker_limits(
    marker: Mapping[str, Any] | None,
    limits: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if marker is None and limits is None:
        return None
    if marker is None or limits is None:
        raise CLIDevelopmentSessionConflict(
            "managed worker run limits are missing",
            code="delegate_worker_limits_mismatch",
        )
    budgets = limits.get("budgets")
    attempt = limits.get("attempt", 1)
    expected_limit_keys = {
        "schema_version",
        "dialogue_id",
        "workspace_scope_sha256",
        "budgets",
        "timeout_seconds",
        "attempt",
    }
    if (
        not set(limits) <= expected_limit_keys
        or not {"schema_version", "dialogue_id", "workspace_scope_sha256", "budgets", "timeout_seconds"} <= set(limits)
        or type(limits.get("schema_version")) is not int
        or limits.get("schema_version") != 1
        or str(limits.get("dialogue_id") or "") != str(marker.get("dialogue_id") or "")
        or str(limits.get("workspace_scope_sha256") or "")
        != str(marker.get("workspace_scope_sha256") or "")
        or not isinstance(marker.get("budgets"), Mapping)
        or not isinstance(budgets, Mapping)
        or any(
            not isinstance(key, str)
            or key not in _DELEGATE_WORKER_BUDGET_LIMITS
            or type(value) is not int
            or value < 1
            for key, value in budgets.items()
        )
        or dict(budgets) != dict(marker.get("budgets") or {})
        or (
            limits.get("timeout_seconds") is not None
            and type(limits.get("timeout_seconds")) is not int
        )
        or limits.get("timeout_seconds") != marker.get("timeout_seconds")
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or attempt > int((marker.get("budgets") or {}).get("max_attempts", 1))
    ):
        raise CLIDevelopmentSessionConflict(
            "managed worker run limits differ from the fixed session scope",
            code="delegate_worker_limits_mismatch",
        )
    return {"attempt": attempt, **_delegate_worker_audit_marker(marker)}


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


def _safe_delegate_worker_usage_receipt(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    usage: dict[str, Any] = {
        str(key): max(0, int(item))
        for key, item in value.items()
        if str(key)
        in {
            "input_tokens",
            "prompt_tokens",
            "output_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "estimated_cost_micros",
            "total_tokens",
            "cost_micros",
            "budget_overrun_tokens",
            "budget_overrun_cost_micros",
        }
        and isinstance(item, int)
        and not isinstance(item, bool)
        and 0 <= item <= 10**15
    }
    if isinstance(value.get("available"), bool):
        usage["available"] = value["available"]
    provider = value.get("provider")
    if isinstance(provider, str) and provider in {"muse-remote", "codex-cli", "antigravity-cli", "claude-cli", "grok-cli"}:
        usage["provider"] = provider
    reason = value.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,63}", reason):
        usage["reason"] = reason
    return usage


def _safe_delegate_worker_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    from .outbound_privacy_service import redact_secret_for_local_display

    result: dict[str, Any] = {}
    summary = value.get("summary")
    if isinstance(summary, str):
        safe_summary = redact_secret_for_local_display(summary[:30_000])
        result["summary"] = str(safe_summary)[:3_000]
    diff = value.get("diff")
    if isinstance(diff, str):
        safe_diff = redact_secret_for_local_display(diff[:30_000])
        result["diff"] = str(safe_diff)[:30_000]
    changed_files = value.get("changed_files")
    if isinstance(changed_files, (list, tuple)):
        safe_paths: list[str] = []
        for item in changed_files[:128]:
            if not isinstance(item, str) or not item or len(item) > 512:
                continue
            path = item.replace("\\", "/")
            if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or any(part in {"", ".", ".."} for part in path.split("/")):
                continue
            safe_paths.append(path)
        result["changed_files"] = list(dict.fromkeys(safe_paths))
    artifact_refs = value.get("artifact_refs")
    if isinstance(artifact_refs, (list, tuple)):
        result["artifact_refs"] = [
            item[:512]
            for item in artifact_refs[:64]
            if isinstance(item, str) and item and len(item) <= 512
        ]
    usage = _safe_delegate_worker_usage_receipt(value.get("usage"))
    if usage is not None:
        result["usage"] = usage
    if isinstance(value.get("change_evidence_available"), bool):
        result["change_evidence_available"] = value["change_evidence_available"]
    evidence_status = value.get("change_evidence_status")
    if evidence_status in {"verified", "read_only", "unverified", "truncated"}:
        result["change_evidence_status"] = evidence_status
    return result


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


def _muse_chat_route(binding: Any) -> Mapping[str, Any]:
    """Return the bound AgentRevision Chat route for Muse admission.

    New v2 snapshots may expose ``chat`` directly. Older immutable snapshots
    use the ``operator`` role, whose v2 mapping is the Chat model role; keep
    that historical spelling readable while requiring the route itself to be
    the typed Muse remote route.
    """

    raw = _binding_value(binding, "route_snapshot")
    roles = raw.get("roles", raw) if isinstance(raw, Mapping) else {}
    route = roles.get("chat") if isinstance(roles, Mapping) else None
    if not isinstance(route, Mapping):
        route = roles.get("operator") if isinstance(roles, Mapping) else None
    if (
        not isinstance(route, Mapping)
        or str(route.get("provider") or "").strip().casefold() != MUSE_PROVIDER
        or str(route.get("backend") or "").strip().casefold() != "remote"
        or route.get("model") not in (None, "", "muse-chat")
        or route.get("effort") not in (None, "")
    ):
        raise CLIDevelopmentSessionConflict(
            "AgentRevision Chat route is not a Muse remote route",
            code="agent_route_mismatch",
        )
    return {
        "provider": MUSE_PROVIDER,
        "model": None,
        "effort": None,
        "backend": "remote",
        "route_source": str(route.get("route_source") or "")[:80],
    }


def _assert_route(binding: Any, *, provider: str, model: str | None, effort: str | None) -> dict[str, Any]:
    routes = [_muse_chat_route(binding)] if provider.strip().casefold() == MUSE_PROVIDER else _executable_routes(binding)
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
    # The pinned P2 role route itself must name a registered CLI adapter.
    from .agent_model_routing import AgentModelRouteError, assert_registered_route_backend

    raw_backend = declared_backends.get(key, "")
    declared = "cli" if not raw_backend or raw_backend in _CLI_BACKEND_ALIASES else raw_backend
    try:
        assert_registered_route_backend(key[0], declared, ingress="cli")
    except AgentModelRouteError as exc:
        code = "cli_route_not_supported" if exc.code == "agent_route_provider_unsupported" else exc.code
        raise CLIDevelopmentSessionConflict("Agent route has no registered CLI adapter", code=code) from None
    return result


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
    role_context_mode: str = "raw"
    execution_target: str | None = None
    provided_fields: frozenset[str] = frozenset()

    @classmethod
    def from_value(cls, value: Any = None, **kwargs: Any) -> "CreateCLIDevelopmentSessionRequest":
        source = dict(value) if isinstance(value, Mapping) else {}
        source.update(kwargs)
        if "execution_profile_id" in source:
            raise CLIDevelopmentSessionInvalid(
                "execution_profile_id is no longer accepted",
                code="agent_execution_profile_removed",
            )
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
            role_context_mode=role_context_mode,
            execution_target=MUSE_EXECUTION_TARGET if remote else execution_target,
            provided_fields=provided,
        )


@dataclass(frozen=True)
class SendCLIDevelopmentSessionRequest:
    owner_id: str
    content: str
    send_key: str
    muse_tool_loop_opt_in: bool = False

    @classmethod
    def from_value(cls, value: Any = None, **kwargs: Any) -> "SendCLIDevelopmentSessionRequest":
        source = dict(value) if isinstance(value, Mapping) else {}
        source.update(kwargs)
        if "content" not in source and "raw" in source:
            source["content"] = source["raw"]
        opt_in = source.get("muse_tool_loop_opt_in", False)
        if not isinstance(opt_in, bool):
            raise CLIDevelopmentSessionInvalid(
                "muse_tool_loop_opt_in must be a boolean",
                code="muse_tool_loop_opt_in_invalid",
            )
        return cls(
            owner_id=_identifier(source.get("owner_id", source.get("user_id")), field_name="owner_id"),
            content=_text(source.get("content"), field_name="content", limit=128_000),
            send_key=_text(source.get("send_key", source.get("idempotency_key")), field_name="send_key", limit=512),
            muse_tool_loop_opt_in=opt_in,
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
        remote_config_supplier: Any | None = None,
    ) -> None:
        self._remote_config_supplier = remote_config_supplier
        self._db_manager = db_manager
        self.config = config
        self.coordinator = coordinator
        self.session_store = session_store
        self.agent_service = agent_service or ConversationAgentService(db_manager, config=config)
        self.authority_resolver = authority_resolver or AgentAuthorityResolver(db_manager, config=config)
        self._clock = clock or _utcnow
        self.remote_adapter: Any | None = None
        self.remote_binding_store: Any | None = None
        _register_shared_cli_development_session_service(self)

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

    def _remote_target_config(self, *, _remote_config_snapshot: Any = None) -> Mapping[str, Any]:
        # Only trusted internal callers may supply a server-owned snapshot.
        config = self.config if _remote_config_snapshot is None else _remote_config_snapshot
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

    async def _resolve_remote_config_snapshot(self, explicit: Any = None) -> Any:
        """Resolve a trusted server snapshot once at an operation boundary."""
        if explicit is not None:
            return explicit
        if self._remote_config_supplier is None:
            return None
        value = self._remote_config_supplier()
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            raise CLIDevelopmentSessionUnavailable(
                "remote configuration is unavailable", code="remote_config_unavailable"
            )
        return value

    def _get_remote_adapter(self, *, _remote_config_snapshot: Any = None) -> Any:
        """Build snapshot-bound adapters; retain the startup adapter cache by default."""
        if _remote_config_snapshot is None and self.remote_adapter is not None:
            return self.remote_adapter
        from .muse_remote_execution import MuseRemoteClient, MuseRemoteExecutionAdapter

        executable = _managed_muse_cli_executable(
            platform=sys.platform, environ=os.environ, home=Path.home()
        )
        self.remote_binding_store = self.remote_binding_store or MuseRemoteSessionStore(
            self._db_manager
        )
        config = self.config if _remote_config_snapshot is None else _remote_config_snapshot
        target = self._remote_target_config(_remote_config_snapshot=_remote_config_snapshot)
        adapter = MuseRemoteExecutionAdapter(
            client=MuseRemoteClient(executable=str(executable)),
            credential_resolver=MuseRemoteTargetResolver(config),
            binding_store=self.remote_binding_store,
            privacy_gateway=MuseRemotePrivacyGateway(config),
            wait_seconds=target.get("wait_seconds", 30.0),
            reconcile_timeout_seconds=target.get("reconcile_timeout_seconds", 120.0),
            poll_interval_seconds=target.get("poll_interval_seconds", 2.0),
        )
        if _remote_config_snapshot is None:
            self.remote_adapter = adapter
        return adapter

    @staticmethod
    def _remote_allowlisted(value: str, raw: Any) -> bool:
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            values = list(raw)
        else:
            values = []
        return bool(values) and value in {str(item).strip() for item in values if str(item).strip()}

    def _assert_remote_admission(
        self, *, owner_id: str, agent_id: str, _remote_config_snapshot: Any = None
    ) -> Mapping[str, Any]:
        target = self._remote_target_config(_remote_config_snapshot=_remote_config_snapshot)
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
        managed_backend: bool = False,
    ) -> dict[str, Any]:
        decisions: dict[str, Any] = {}
        for capability in tuple(dict.fromkeys(str(item).strip() for item in capabilities if str(item).strip())):
            try:
                backend_options = (
                    {"_backend_execution_without_agent_tools": True}
                    if managed_backend else {}
                )
                decision = await self.authority_resolver.resolve(
                    agent_id=agent_id,
                    revision_id=revision_id,
                    project_id=project_id,
                    required_capability=capability,
                    tool_capabilities=tuple(capabilities),
                    harness_capabilities=tuple(capabilities),
                    **backend_options,
                )
            except Exception as exc:
                raise CLIDevelopmentSessionUnavailable("Agent authority is unavailable", code="agent_authority_unavailable") from exc
            decisions[capability] = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision) if isinstance(decision, Mapping) else {"allowed": bool(getattr(decision, "allowed", False))}
            if not _decision_allowed(decision):
                reason = getattr(decision, "reason", None) or decisions[capability].get("reason") or "agent_authority_denied"
                raise CLIDevelopmentSessionForbidden("Agent authority denied", code=f"agent_authority_{str(reason)[:80]}")
        return decisions

    async def _verified_managed_backend(self, db: Any, row: Any, context: Mapping[str, Any]) -> bool:
        marker = context.get("delegate_worker")
        if marker is None:
            return False
        if not isinstance(marker, Mapping) or _safe_uuid(marker.get("dialogue_id")) is None:
            raise CLIDevelopmentSessionForbidden("managed worker marker is invalid", code="delegate_worker_context_invalid")
        from ..memory.models import AgentDialogue
        from .delegate_worker_runtime import DelegateWorkerRuntime

        try:
            verified = await DelegateWorkerRuntime(cli_session_service=self, config=self.config)._validate_dialogue_session(
                str(row.id), str(marker["dialogue_id"]), owner_id=str(row.user_id), _db_session=db,
            )
            dialogue = await db.get(AgentDialogue, uuid.UUID(str(marker["dialogue_id"])))
            if (
                any(marker.get(key) != verified.get(key) for key in (
                    "dialogue_id", "kind", "workspace_scope", "workspace_scope_sha256",
                    "timeout_seconds", "budgets", "owner_id", "agent_id", "agent_revision_id",
                    "allow_cli_native_tools",
                ))
                or str(dialogue.project_id or "") != str(context.get("project_id") or "")
                or str(marker.get("agent_id") or "") != str(context.get("agent_id") or row.agent_id or "")
                or str(marker.get("agent_revision_id") or "") != str(context.get("agent_revision_id") or "")
                or sorted(marker.get("capabilities") or []) != sorted(
                    REMOTE_SESSION_CAPABILITIES
                    if context.get("provider") == MUSE_PROVIDER
                    and context.get("allowed_capabilities") == list(REMOTE_SESSION_CAPABILITIES)
                    else pinned_cli_capabilities(context)
                )
                or marker.get("agent_dialogue_tool_ids") != []
            ):
                raise ValueError("managed worker binding mismatch")
        except Exception as exc:
            raise CLIDevelopmentSessionForbidden("managed worker binding is invalid", code="delegate_worker_dialogue_binding_invalid") from exc
        return True

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
        artifact_complete = run_result.get("assistant_artifact_complete")
        if not isinstance(artifact_complete, bool):
            artifact_complete = work_result.get("assistant_artifact_complete")
        raw_observation = run_result.get("muse_tool_loop_history_observation")
        observation_fields = (
            "schema_version",
            "status",
            "run_id",
            "work_item_id",
            "binding_id",
            "binding_snapshot_hash",
            "turn_nonce_sha256",
            "provider_assistant_message_id",
            "assistant_text_sha256",
            "assistant_text_length",
            "tool_dispatch",
            "tool_receipts",
            "work_budget",
            "observed_at",
        )
        observation = (
            {
                key: raw_observation[key]
                for key in observation_fields
                if key in raw_observation
            }
            if isinstance(raw_observation, Mapping)
            and raw_observation.get("status") == "tool_loop_recovery_required"
            else None
        )
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
            "assistant_artifact_complete": (
                artifact_complete if isinstance(artifact_complete, bool) else None
            ),
            "history_observation": observation,
        }

    @staticmethod
    def _remote_cli_failure_projection(
        *, row: Any, run: Any | None, work: Any | None,
        context: Mapping[str, Any], owner_id: str,
    ) -> dict[str, Any] | None:
        """Return cause labels only after the complete remote record tuple matches."""

        if (
            run is None
            or work is None
            or str(context.get("provider") or "").strip().lower() != MUSE_PROVIDER
        ):
            return None
        session_id = str(getattr(row, "id", "") or "")
        run_id = str(getattr(run, "id", "") or "")
        work_id = str(getattr(work, "id", "") or "")
        agent_id = str(context.get("agent_id") or "")
        revision_id = str(context.get("agent_revision_id") or "")
        binding_id = str(context.get("binding_id") or "")
        binding_hash = str(context.get("binding_snapshot_hash") or "")
        run_metadata = getattr(run, "run_metadata", None)
        run_metadata = run_metadata if isinstance(run_metadata, Mapping) else {}
        cli_metadata = run_metadata.get("cli_development")
        cli_metadata = cli_metadata if isinstance(cli_metadata, Mapping) else {}
        work_metadata = getattr(work, "metadata_json", None)
        work_metadata = work_metadata if isinstance(work_metadata, Mapping) else {}
        row_context = getattr(row, "context", None)
        row_context = row_context if isinstance(row_context, Mapping) else {}
        remote_context = row_context.get(MUSE_REMOTE_CONTEXT_KEY)
        remote_binding = (
            remote_context.get("binding")
            if isinstance(remote_context, Mapping)
            and isinstance(remote_context.get("binding"), Mapping)
            else remote_context
        )
        if not isinstance(remote_binding, Mapping):
            return None

        # The IDs are checked here but are never copied into ``cli_failure``.
        if (
            not all((session_id, run_id, work_id, agent_id, revision_id, binding_id, binding_hash))
            or str(getattr(row, "user_id", "") or "") != owner_id
            or str(context.get("owner_id") or "") != owner_id
            or str(getattr(run, "user_id", "") or "") != owner_id
            or str(work_metadata.get("owner_id") or "") != owner_id
            or str(getattr(row, "agent_id", "") or "") != agent_id
            or str(getattr(run, "session_id", "") or "") != session_id
            or str(getattr(run, "work_item_id", "") or "") != work_id
            or str(getattr(run, "run_type", "") or "") != REMOTE_SESSION_SOURCE_TYPE
            or str(getattr(run, "agent_id", "") or "") != agent_id
            or str(getattr(run, "agent_revision_id", "") or "") != revision_id
            or str(getattr(work, "conversation_session_id", "") or "") != session_id
            or str(getattr(work, "source_id", "") or "") != run_id
            or str(getattr(work, "source_revision", "") or "") != revision_id
            or str(getattr(work, "source_type", "") or "") != REMOTE_SESSION_SOURCE_TYPE
            or str(getattr(work, "execution_adapter", "") or "") != MUSE_REMOTE_ADAPTER_KEY
            or str(getattr(work, "assigned_agent_id", "") or "") != agent_id
            or str(getattr(work, "agent_revision_id", "") or "") != revision_id
            or str(work_metadata.get("binding_id") or "") != binding_id
            or str(work_metadata.get("binding_snapshot_hash") or "") != binding_hash
            or str(work_metadata.get("remote_target_key") or "") != session_id
            or str(cli_metadata.get("binding_id") or "") != binding_id
            or str(cli_metadata.get("binding_snapshot_hash") or "") != binding_hash
            or str(cli_metadata.get("remote_target_key") or "") != session_id
            or str(remote_binding.get("owner_id") or "") != owner_id
            or str(remote_binding.get("agent_id") or "") != agent_id
            or str(remote_binding.get("revision_id") or "") != revision_id
            or str(remote_binding.get("remote_target_key") or "") != session_id
        ):
            return None

        run_result = getattr(run, "result", None)
        run_result = run_result if isinstance(run_result, Mapping) else {}
        work_result = getattr(work, "result_summary_json", None)
        work_result = work_result if isinstance(work_result, Mapping) else {}
        run_cause = sanitize_cause(run_result.get("cause"))
        work_cause = sanitize_cause(work_result.get("cause"))
        if run_cause is not None and work_cause is not None and run_cause != work_cause:
            return None
        cause = run_cause or work_cause
        if cause is None:
            return None
        command = cause.get("command")
        if command is None:
            return None
        bound_run_id = str(remote_binding.get("current_run_id") or "")
        run_status = str(getattr(run, "status", "") or "")
        work_state = str(getattr(work, "state", "") or "")
        outcome_classification = str(getattr(work, "outcome_classification", "") or "")
        unsafe_failure = (
            run_status == "failed"
            and work_state in {"blocked", "uncertain", "failed", "dead_letter"}
            and outcome_classification != "succeeded"
            and str(remote_binding.get("state") or "") == "unsafe"
            and remote_binding.get("safe_to_send") is False
        )
        retryable_history_failure = (
            run_status == "failed"
            and work_state in {"retry_wait", "dead_letter"}
            and outcome_classification == "transient"
            and command == "history"
            and str(remote_binding.get("state") or "") == "bound"
            and remote_binding.get("safe_to_send") is True
            and not bound_run_id
        )
        if not (unsafe_failure or retryable_history_failure):
            return None
        if command == "send" and bound_run_id != run_id:
            return None
        if bound_run_id and bound_run_id != run_id:
            return None

        return {
            "classification": "cli_failure",
            "cause_code": cause.get("cause_code"),
            "command": command,
            "stderr_class": cause.get("stderr_class"),
            "stage": cause.get("stage"),
            "phase_class": cause.get("phase_class"),
            "exception_type": cause.get("exception_type") or cause.get("cause_type"),
            "returncode": cause.get("returncode"),
            "duration_ms": cause.get("duration_ms"),
            "curl_code": cause.get("curl_code"),
            "http_status": cause.get("http_status"),
            "gateway_status": cause.get("gateway_status"),
            "timed_out": cause.get("timed_out"),
        }

    @staticmethod
    def _sanitize_cause_in_result_projection(value: Any, *, result_key: str) -> Any:
        """Sanitize bounded Muse failure evidence in a copied result projection."""

        if not isinstance(value, Mapping):
            return value
        projection = dict(value)
        result = projection.get(result_key)
        if isinstance(result, Mapping) and (
            "cause" in result or "cli_failure_diagnostic" in result
        ):
            safe_result = dict(result)
            if "cause" in safe_result:
                cause = sanitize_cause(safe_result.get("cause"))
                if cause is None:
                    safe_result.pop("cause", None)
                else:
                    safe_result["cause"] = cause
            if "cli_failure_diagnostic" in safe_result:
                diagnostic = sanitize_cli_failure_diagnostic(
                    safe_result.get("cli_failure_diagnostic")
                )
                if diagnostic is None:
                    safe_result.pop("cli_failure_diagnostic", None)
                else:
                    safe_result["cli_failure_diagnostic"] = diagnostic
            projection[result_key] = safe_result
        return projection

    @staticmethod
    def _muse_authorized_scope_store() -> Any | None:
        """Resolve the server-owned Muse scope store without accepting caller input.

        The approval store is deliberately looked up by the service boundary so
        both the coordinator callback and the read-only status projection use
        the same owner-bound source.  A missing store is a fail-closed
        condition; it never turns an authorized request into the QA default.
        """

        for module_name in ("muse_authorized_scope", "muse_authorized_scope_store"):
            try:
                module = importlib.import_module(f".{module_name}", __package__)
                factory = getattr(module, "MuseAuthorizedScopeStore", None)
                if callable(factory):
                    return factory()
            except Exception:
                continue
        return None

    @classmethod
    async def _read_active_muse_authorized_scope(
        cls,
        session: Any,
        identity: Any,
        *,
        session_id: str | None = None,
        binding_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Read one active approved scope from the server-owned store.

        ``identity`` is created from the immutable Agent binding by the caller;
        this method does not inspect model text, request bodies, or a user
        supplied workspace.  Store errors and malformed values fail closed.
        """

        if identity is None:
            return None
        if not session_id or not binding_id:
            raise RuntimeError("muse_authorized_scope_context_unavailable")
        store = cls._muse_authorized_scope_store()
        if store is None:
            raise RuntimeError("muse_authorized_scope_store_unavailable")
        reader = getattr(store, "read_active_for_identity", None)
        if not callable(reader):
            raise RuntimeError("muse_authorized_scope_reader_unavailable")
        value = reader(
            session,
            identity,
            session_id=session_id,
            binding_id=binding_id,
        )
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("muse_authorized_scope_record_invalid")
        return dict(value)

    async def _muse_authorized_scope_for_context(
        self,
        session: Any,
        *,
        context: Mapping[str, Any],
        owner_id: str,
        session_id: str,
        binding_id: str,
    ) -> Mapping[str, Any] | None:
        """Resolve the active approved scope using only the immutable context."""

        try:
            from .cli_session_work_adapter import muse_authorized_scope_identity

            identity = muse_authorized_scope_identity(
                owner_id=owner_id,
                agent_id=str(context.get("agent_id") or ""),
                revision_id=str(context.get("agent_revision_id") or ""),
                binding_snapshot_hash=str(context.get("binding_snapshot_hash") or ""),
            )
        except Exception:
            return None
        try:
            return await self._read_active_muse_authorized_scope(
                session,
                identity,
                session_id=session_id,
                binding_id=binding_id,
            )
        except Exception:
            # Status projection is read-only and may omit diagnostics when the
            # approval store is unavailable.  The execution callback uses the
            # same helper directly, so it still blocks instead of falling back
            # to the QA scope.
            return None

    def _muse_tool_loop_diagnostics(
        self,
        *,
        row: Any,
        run: Any | None,
        work: Any | None,
        context: Mapping[str, Any],
        owner_id: str,
        authorized_scope_record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Flatten owner/run-scoped Muse receipt facts without exposing output.

        QA diagnostics keep their fixed repository scope.  Authorized
        diagnostics reconstruct the workspace and tool allowlist only from the
        active server-owned scope record and require its persisted id/digest to
        match the exact plan receipt.
        """

        if (
            run is None
            or work is None
            or not self._is_remote_context(context)
            or str(context.get("provider") or "").strip().lower() != MUSE_PROVIDER
        ):
            return None
        session_id = str(getattr(row, "id", "") or "")
        run_id = str(getattr(run, "id", "") or "")
        work_id = str(getattr(work, "id", "") or "")
        agent_id = str(context.get("agent_id") or getattr(row, "agent_id", "") or "")
        revision_id = str(context.get("agent_revision_id") or "")
        binding_id = str(context.get("binding_id") or "")
        binding_snapshot_hash = str(context.get("binding_snapshot_hash") or "")
        if (
            not all((session_id, run_id, work_id, agent_id, revision_id, binding_id, binding_snapshot_hash))
            or str(getattr(row, "user_id", "") or "") != owner_id
            or str(getattr(run, "user_id", "") or "") != owner_id
            or str(getattr(run, "session_id", "") or "") != session_id
            or str(getattr(run, "work_item_id", "") or "") != work_id
            or str(getattr(run, "run_type", "") or "") != REMOTE_SESSION_SOURCE_TYPE
            or str(getattr(work, "conversation_session_id", "") or "") != session_id
            or str(getattr(work, "source_id", "") or "") != run_id
            or str(getattr(work, "source_type", "") or "") != REMOTE_SESSION_SOURCE_TYPE
            or str(getattr(work, "execution_adapter", "") or "") != MUSE_REMOTE_ADAPTER_KEY
            or str(getattr(work, "assigned_agent_id", "") or "") != agent_id
            or str(getattr(work, "agent_revision_id", "") or "") != revision_id
        ):
            return None

        run_metadata = getattr(run, "run_metadata", None)
        run_metadata = run_metadata if isinstance(run_metadata, Mapping) else {}
        cli_metadata = run_metadata.get("cli_development")
        cli_metadata = cli_metadata if isinstance(cli_metadata, Mapping) else {}
        work_metadata = getattr(work, "metadata_json", None)
        work_metadata = work_metadata if isinstance(work_metadata, Mapping) else {}
        if (
            work_metadata.get("owner_id") != owner_id
            or _matching_muse_tool_loop_opt_in(work_metadata, cli_metadata) is not True
            or str(work_metadata.get("binding_id") or "") != binding_id
            or str(work_metadata.get("binding_snapshot_hash") or "") != binding_snapshot_hash
            or str(cli_metadata.get("binding_id") or "") != binding_id
            or str(cli_metadata.get("binding_snapshot_hash") or "") != binding_snapshot_hash
        ):
            return None

        summary = getattr(work, "result_summary_json", None)
        summary = summary if isinstance(summary, Mapping) else {}
        plan_receipt = summary.get("muse_tool_loop_scope")
        work_loop_summary = summary.get("muse_tool_loop")
        if not isinstance(plan_receipt, Mapping):
            return None
        known_tools = frozenset({"read_file", "edit_file", "execute_command"})

        def valid_loop_receipt_count(candidate: Any) -> int:
            if not isinstance(candidate, Mapping):
                return 0
            receipts = candidate.get("receipts")
            if not isinstance(receipts, list):
                return 0
            return sum(
                1
                for item in receipts
                if isinstance(item, Mapping)
                and isinstance(item.get("operation_id"), str)
                and re.fullmatch(r"[a-f0-9]{64}", item["operation_id"])
                and item.get("tool") in known_tools
            )

        loop_summary = work_loop_summary if isinstance(work_loop_summary, Mapping) else {}
        loop_receipt_source = "work_summary" if isinstance(work_loop_summary, Mapping) else None
        run_result = getattr(run, "result", None)
        run_result = run_result if isinstance(run_result, Mapping) else {}
        run_loop_summary = run_result.get("muse_tool_loop")
        if isinstance(run_loop_summary, Mapping):
            summaries_match = True
            if isinstance(work_loop_summary, Mapping):
                summaries_match = all(
                    work_loop_summary.get(key) is None
                    or run_loop_summary.get(key) is None
                    or work_loop_summary.get(key) == run_loop_summary.get(key)
                    for key in ("status", "error_code", "round_count", "tool_call_count")
                )
            if (
                summaries_match
                and valid_loop_receipt_count(run_loop_summary)
                > valid_loop_receipt_count(work_loop_summary)
            ):
                # AgentWorkItem's generic safe JSON projection is depth capped;
                # nested receipts can become empty objects. AgentRun.result is
                # bound to this exact owner/session/run/work tuple above and
                # retains the adapter's bounded loop receipt summary.
                loop_summary = run_loop_summary
                loop_receipt_source = "run_result"
        loop_summary_present = loop_receipt_source is not None
        raw_tools = plan_receipt.get("allowed_tools")
        if not isinstance(raw_tools, (list, tuple, set, frozenset)):
            return None

        mode = plan_receipt.get("mode")
        authorized_scope = None
        if mode == "qa_default":
            expected_tools = known_tools
            if (
                plan_receipt.get("authorized_scope") != "unconfigured"
                or frozenset(raw_tools) != expected_tools
            ):
                return None
        elif mode == "authorized_scope":
            if plan_receipt.get("authorized_scope") != "authorized" or authorized_scope_record is None:
                return None
            try:
                from .cli_session_work_adapter import muse_authorized_scope_identity
                from .muse_authorized_scope import build_muse_authorized_scope

                identity = muse_authorized_scope_identity(
                    owner_id=owner_id,
                    agent_id=agent_id,
                    revision_id=revision_id,
                    binding_snapshot_hash=binding_snapshot_hash,
                )
                if identity is None:
                    return None
                authorized_scope = build_muse_authorized_scope(
                    authorized_scope_record,
                    expected_identity=identity,
                )
            except Exception:
                return None
            expected_tools = frozenset(authorized_scope.allowed_tools)
            if (
                not expected_tools
                or not expected_tools.issubset(known_tools)
                or frozenset(raw_tools) != expected_tools
                or plan_receipt.get("authorized_scope_id") != authorized_scope.scope_id
                or plan_receipt.get("authorized_scope_digest") != authorized_scope.digest
            ):
                return None
        else:
            return None

        try:
            from .cli_session_work_adapter import _muse_base_authority_revision
            from .muse_scoped_tool_execution import (
                MuseToolExecutionScope,
                read_muse_tool_receipt_diagnostics,
            )

            managed_repository_root = Path(
                getattr(self, "_muse_tool_loop_repository_root", None)
                or Path(__file__).resolve().parents[2]
            ).resolve(strict=True)
            repository_root = (
                authorized_scope.workspace_root
                if authorized_scope is not None else managed_repository_root
            )
            authority_revision = _muse_base_authority_revision(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
                session_id=session_id,
                run_id=run_id,
                binding_id=binding_id,
                binding_snapshot_hash=binding_snapshot_hash,
                allowed_tools=expected_tools,
            )
            if authorized_scope is not None:
                from .muse_authorized_scope import build_muse_authorized_execution_scope

                scope = build_muse_authorized_execution_scope(
                    authorized_scope,
                    session_id=session_id,
                    run_id=run_id,
                    base_authority_revision=authority_revision,
                )
            else:
                scope = MuseToolExecutionScope(
                    owner_id=owner_id,
                    session_id=session_id,
                    run_id=run_id,
                    workspace_root=repository_root,
                    allowed_tools=expected_tools,
                    authority_revision=authority_revision,
                )
            from .muse_journal_path import _ready_muse_journal

            journal_path = _ready_muse_journal(repository_root, managed_repository_root=managed_repository_root)
            executor = read_muse_tool_receipt_diagnostics(
                journal_path, scope, expected_internal_journal_path=journal_path,
            )
        except Exception:
            executor = {"status": "journal_unavailable", "receipts": []}

        raw_loop_receipts = loop_summary.get("receipts")
        loop_receipts = raw_loop_receipts if isinstance(raw_loop_receipts, list) else []
        loop_by_id = {
            item["operation_id"]: item
            for item in loop_receipts
            if isinstance(item, Mapping)
            and isinstance(item.get("operation_id"), str)
            and re.fullmatch(r"[a-f0-9]{64}", item["operation_id"])
            and item.get("tool") in expected_tools
        }
        raw_executor_receipts = executor.get("receipts")
        executor_receipts = raw_executor_receipts if isinstance(raw_executor_receipts, list) else []
        executor_by_id = {
            item["operation_id"]: item
            for item in executor_receipts
            if isinstance(item, Mapping)
            and isinstance(item.get("operation_id"), str)
            and item.get("tool") in expected_tools
        }
        operations: list[dict[str, Any]] = []
        operation_ids = list(loop_by_id)
        operation_ids.extend(operation_id for operation_id in executor_by_id if operation_id not in loop_by_id)
        for operation_id in operation_ids[:32]:
            loop_receipt = loop_by_id.get(operation_id)
            executor_receipt = executor_by_id.get(operation_id)
            if loop_receipt is not None and executor_receipt is not None:
                loop_call_hash = loop_receipt.get("call_id_sha256")
                executor_call_hash = executor_receipt.get("call_id_sha256")
                if (
                    loop_receipt.get("tool") != executor_receipt.get("tool")
                    or loop_receipt.get("status") != executor_receipt.get("status")
                    or (
                        isinstance(loop_call_hash, str)
                        and isinstance(executor_call_hash, str)
                        and loop_call_hash != executor_call_hash
                    )
                ):
                    correlation = "receipt_mismatch"
                elif isinstance(loop_call_hash, str) and isinstance(executor_call_hash, str):
                    correlation = "matched"
                else:
                    correlation = "operation_id_matched_call_id_digest_missing"
            elif loop_receipt is not None:
                correlation = "loop_receipt_only"
            else:
                correlation = "executor_receipt_only"

            executor_state = executor_receipt.get("state") if executor_receipt else None
            executed = executor_receipt.get("executed") if executor_receipt else None
            return_code = (
                executor_receipt.get("command_return_code")
                if executor_receipt and type(executor_receipt.get("command_return_code")) is int
                else None
            )
            if executor_receipt is None:
                execution_evidence = "receipt_missing"
            elif executor_state in {"admitted", "aborted"}:
                execution_evidence = "not_dispatched"
            elif executor_state in {"started", "uncertain"}:
                execution_evidence = "dispatch_unconfirmed"
            elif executor_state == "completed" and executed is False:
                execution_evidence = "router_completed_without_execution"
            elif executor_state == "completed" and (loop_receipt or executor_receipt).get("tool") == "execute_command" and executor_receipt.get("command_timed_out") is True:
                execution_evidence = "command_timed_out"
            elif executor_state == "completed" and (loop_receipt or executor_receipt).get("tool") == "execute_command" and return_code is None:
                execution_evidence = "child_return_code_unknown"
            elif executor_state == "completed" and (loop_receipt or executor_receipt).get("tool") == "execute_command":
                execution_evidence = "child_completed"
            elif executor_state == "completed" and executed is True:
                execution_evidence = "tool_completed"
            else:
                execution_evidence = "unknown"

            operations.append(
                {
                    "operation_id": operation_id,
                    "call_id_sha256": (
                        loop_receipt.get("call_id_sha256")
                        if loop_receipt
                        and isinstance(loop_receipt.get("call_id_sha256"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", loop_receipt["call_id_sha256"])
                        else None
                    ),
                    "executor_call_id_sha256": (
                        executor_receipt.get("call_id_sha256") if executor_receipt else None
                    ),
                    "tool": (loop_receipt or executor_receipt).get("tool"),
                    "correlation": correlation,
                    "execution_evidence": execution_evidence,
                    "loop_status": (
                        loop_receipt.get("status")
                        if loop_receipt and loop_receipt.get("status") in {
                            "completed", "rejected", "cancelled", "conflict", "fenced", "uncertain"
                        }
                        else None
                    ),
                    "loop_success": (
                        loop_receipt.get("success")
                        if loop_receipt and isinstance(loop_receipt.get("success"), bool)
                        else None
                    ),
                    "loop_executed": (
                        loop_receipt.get("executed")
                        if loop_receipt and isinstance(loop_receipt.get("executed"), bool)
                        else None
                    ),
                    "mutation_epoch": (
                        loop_receipt.get("mutation_epoch")
                        if loop_receipt
                        and isinstance(loop_receipt.get("mutation_epoch"), int)
                        and not isinstance(loop_receipt.get("mutation_epoch"), bool)
                        else None
                    ),
                    "executor_state": executor_state,
                    "executor_status": executor_receipt.get("status") if executor_receipt else None,
                    "executor_success": executor_receipt.get("success") if executor_receipt else None,
                    "executor_executed": executed,
                    "blocked_reason": (
                        executor_receipt.get("blocked_reason")[:300]
                        if executor_receipt
                        and isinstance(executor_receipt.get("blocked_reason"), str)
                        else None
                    ),
                    "output_truncated": executor_receipt.get("output_truncated") if executor_receipt else None,
                    "error_truncated": executor_receipt.get("error_truncated") if executor_receipt else None,
                    "admitted_at": executor_receipt.get("admitted_at") if executor_receipt else None,
                    "started_at": executor_receipt.get("started_at") if executor_receipt else None,
                    "operation_updated_at": executor_receipt.get("operation_updated_at") if executor_receipt else None,
                    "receipt_terminal_at": (
                        executor_receipt.get("operation_updated_at")
                        if executor_receipt and executor_state in {"completed", "uncertain", "aborted"}
                        else None
                    ),
                    "command_return_code": return_code,
                    "command_timed_out": executor_receipt.get("command_timed_out") if executor_receipt else None,
                    "receipt_present": executor_receipt is not None,
                }
            )

        loop_status = loop_summary.get("status")
        if loop_status not in {"running", "completed", "rejected", "uncertain", "cancelled", "exhausted"}:
            loop_status = None
        error_code = loop_summary.get("error_code")
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z0-9_]{1,128}", error_code):
            error_code = None
        successful_tools = {
            item.get("tool")
            for item in operations
            if item.get("correlation") == "matched"
            and item.get("loop_success") is True
            and item.get("loop_executed") is True
            and item.get("executor_success") is True
            and item.get("executor_executed") is True
            and item.get("output_truncated") is False
            and item.get("error_truncated") is False
            and item.get("tool") in expected_tools
        }
        edit_epochs = [
            item.get("mutation_epoch")
            for item in operations
            if item.get("correlation") == "matched"
            and item.get("tool") == "edit_file"
            and item.get("loop_success") is True
            and item.get("loop_executed") is True
            and item.get("executor_success") is True
            and item.get("executor_executed") is True
            and type(item.get("mutation_epoch")) is int
        ]
        latest_mutation_epoch = max(edit_epochs) if edit_epochs else None
        command_operations = [item for item in operations if item.get("tool") == "execute_command"]
        verification = command_operations[-1] if command_operations else None
        verification_epoch = verification.get("mutation_epoch") if verification else None
        if not loop_summary_present:
            requirements = "loop_summary_missing"
        elif not expected_tools.issubset(successful_tools):
            requirements = "required_tool_success_missing"
        elif verification is None:
            requirements = "verification_receipt_missing"
        elif verification.get("command_timed_out") is True:
            requirements = "command_timed_out"
        elif verification.get("command_return_code") is None:
            requirements = "command_return_code_unknown"
        elif verification["command_return_code"] != 0:
            requirements = "command_nonzero_exit"
        elif verification.get("correlation") != "matched":
            requirements = "receipt_mapping_mismatch"
        elif verification.get("executor_executed") is not True or verification.get("output_truncated") is True or verification.get("error_truncated") is True:
            requirements = "verification_receipt_incomplete"
        elif latest_mutation_epoch is not None and verification_epoch != latest_mutation_epoch:
            requirements = "test_epoch_mismatch"
        else:
            requirements = "required_evidence_present"

        round_count = loop_summary.get("round_count")
        if isinstance(round_count, bool) or not isinstance(round_count, int) or round_count < 0:
            round_count = None
        tool_call_count = loop_summary.get("tool_call_count")
        if isinstance(tool_call_count, bool) or not isinstance(tool_call_count, int) or tool_call_count < 0:
            tool_call_count = None
        return {
            "schema_version": 1,
            "status": executor.get("status") or "unavailable",
            "run_id": run_id,
            "session_id": session_id,
            "work_item_id": work_id,
            "loop_status": loop_status,
            "error_code": error_code,
            "loop_receipt_source": loop_receipt_source,
            "loop_summary_present": loop_summary_present,
            "round_count": round_count,
            "tool_call_count": tool_call_count,
            "successful_tools": sorted(successful_tools),
            "executor_journal_status": executor.get("status") or "unavailable",
            "latest_mutation_epoch": latest_mutation_epoch,
            "verification_epoch": verification_epoch,
            "requirements_diagnosis": requirements,
            "operations": operations,
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

    async def _resolve_remote_backend_execution(
        self, db: Any, row: Any, context: Mapping[str, Any], *, _remote_config_snapshot: Any = None
    ) -> dict[str, Any]:
        """Authorize only the persisted, owned canonical Muse backend route."""
        agent_id = str(context.get("agent_id") or "")
        revision_id = str(context.get("agent_revision_id") or "")
        if (
            context != self._context(row)
            or context.get("provider") != MUSE_PROVIDER
            or context.get("execution_target") != MUSE_EXECUTION_TARGET
            or context.get("remote_target_key") != str(row.id)
            or context.get("allowed_capabilities") != list(REMOTE_SESSION_CAPABILITIES)
            or not agent_id or agent_id != str(row.agent_id or "")
            or not revision_id
            or str(context.get("owner_id") or "") != str(row.user_id)
        ):
            raise CLIDevelopmentSessionForbidden("remote session binding is invalid", code="session_binding_unsafe")
        self._assert_remote_admission(owner_id=str(row.user_id), agent_id=agent_id, _remote_config_snapshot=_remote_config_snapshot)
        verified_managed = await self._verified_managed_backend(db, row, context)
        from ..memory.models import User
        from .conversation_agent_service import ConversationAgentError
        try:
            owner = await db.get(User, uuid.UUID(str(row.user_id)))
            if owner is None or not owner.is_active or str(row.project_id or "") != str(context.get("project_id") or ""):
                raise ValueError("owner or project mismatch")
            _, current_revision = await self.agent_service.validate_agent_selection(
                agent_id=agent_id, actor_user_id=str(row.user_id), actor_role=owner.role,
                project_id=context.get("project_id"), db_session=db,
            )
            if not verified_managed and str(current_revision.id) != revision_id:
                raise ValueError("Agent revision changed")
        except (ConversationAgentError, ValueError) as exc:
            raise CLIDevelopmentSessionForbidden("remote Agent selection is invalid", code="agent_selection_denied") from exc
        except Exception as exc:
            raise CLIDevelopmentSessionUnavailable("remote Agent selection is unavailable", code="agent_selection_unavailable") from exc
        binding = await self._binding_for_row(db, row)
        if any((
            str(_binding_value(binding, "binding_id") or "") != str(context.get("binding_id") or ""),
            str(_binding_value(binding, "snapshot_hash") or "") != str(context.get("binding_snapshot_hash") or ""),
            not context.get("binding_id") or not context.get("binding_snapshot_hash"),
            str(_binding_value(binding, "session_id") or "") != str(row.id),
            str(_binding_value(binding, "agent_id") or "") != agent_id,
            str(_binding_value(binding, "agent_revision_id") or "") != revision_id,
            str(_binding_value(binding, "created_by") or "") != str(row.user_id),
        )):
            raise CLIDevelopmentSessionForbidden("remote session binding is invalid", code="session_binding_unsafe")
        _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
        return await self._resolve_authority(
            agent_id=agent_id, revision_id=revision_id, project_id=context.get("project_id"),
            capabilities=REMOTE_SESSION_CAPABILITIES, managed_backend=True,
        )

    async def _resolve_remote_work_authority(self, db: Any, claim: Any, *, _remote_config_snapshot: Any = None) -> None:
        """Revalidate a canonical Muse Work's durable session and backend authority."""
        from ..memory.models import AgentRun, AgentWorkItem, ConversationSession

        work = await db.get(AgentWorkItem, uuid.UUID(claim.work_item_id))
        if work is None or work.conversation_session_id is None:
            raise ValueError("remote Work is missing")
        row = await db.get(ConversationSession, work.conversation_session_id)
        run = await db.get(AgentRun, work.active_agent_run_id) if work.active_agent_run_id else None
        metadata = work.metadata_json if isinstance(work.metadata_json, Mapping) else {}
        if row is None or run is None or getattr(row, "deleted_at", None) is not None:
            raise ValueError("remote Work binding is missing")
        context = self._context(row)
        if any((
            claim.source_type != REMOTE_SESSION_SOURCE_TYPE,
            claim.execution_adapter != REMOTE_SESSION_SOURCE_TYPE,
            work.source_type != claim.source_type,
            work.execution_adapter != claim.execution_adapter,
            str(work.source_id) != claim.source_id or str(run.id) != claim.source_id,
            str(work.source_revision) != claim.source_revision,
            claim.source_revision != str(claim.agent_revision_id),
            str(work.agent_revision_id) != str(claim.agent_revision_id),
            str(work.assigned_agent_id) != str(claim.assigned_agent_id),
            str(work.conversation_session_id) != str(claim.conversation_session_id),
            str(work.project_id or "") != str(claim.project_id or ""),
            str(work.space_id or "") != str(claim.space_id or ""),
            str(work.persona_id or "") != str(claim.persona_id or ""),
            str(work.task_id or "") != str(claim.task_id or ""),
            str(work.app_id or "") != str(claim.app_id or ""),
            tuple(work.required_capabilities_json or ()) != claim.required_capabilities,
            claim.required_capabilities != tuple(REMOTE_SESSION_CAPABILITIES),
            str(run.work_item_id) != str(work.id),
            str(run.session_id) != str(row.id),
            run.run_type != REMOTE_SESSION_SOURCE_TYPE,
            str(run.agent_id) != str(work.assigned_agent_id),
            str(run.agent_revision_id) != str(work.agent_revision_id),
            str(run.user_id) != str(row.user_id),
            str(metadata.get("owner_id") or "") != str(row.user_id),
            str(metadata.get("remote_target_key") or "") != str(row.id),
            str(context.get("agent_id") or "") != str(work.assigned_agent_id),
            str(context.get("agent_revision_id") or "") != str(work.agent_revision_id),
            str(context.get("project_id") or "") != str(work.project_id or ""),
        )):
            raise ValueError("remote Work binding mismatch")
        _remote_config_snapshot = await self._resolve_remote_config_snapshot(_remote_config_snapshot)
        await self._resolve_remote_backend_execution(db, row, context, _remote_config_snapshot=_remote_config_snapshot)

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

    async def _create_remote_session(
        self,
        req: CreateCLIDevelopmentSessionRequest,
        *,
        session_id: uuid.UUID,
        wire_fingerprint: str,
        delegate_worker_marker: Mapping[str, Any] | None = None,
        _remote_config_snapshot: Any = None,
    ) -> dict[str, Any]:
        """Admit one typed Muse route without entering local CLI policy."""

        self._assert_remote_admission(owner_id=req.owner_id, agent_id=req.agent_id, _remote_config_snapshot=_remote_config_snapshot)
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
        delegate_worker_marker = _validate_delegate_worker_context(
            delegate_worker_marker,
            req,
            agent_id=agent_id,
            revision_id=revision_id,
        )
        if delegate_worker_marker is not None and req.agent_revision_id != revision_id:
            raise CLIDevelopmentSessionConflict(
                "managed worker sessions require an exact AgentRevision pin",
                code="delegate_worker_revision_pin_required",
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
                config=self.config if _remote_config_snapshot is None else _remote_config_snapshot,
                created_by=req.owner_id,
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
            managed_backend=True,
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
        if delegate_worker_marker is not None:
            context_tuple["delegate_worker"] = dict(delegate_worker_marker)
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
        if delegate_worker_marker is not None:
            context_tuple["delegate_worker"] = delegate_worker_marker
            deployment_material["delegate_worker_scope_sha256"] = delegate_worker_marker[
                "workspace_scope_sha256"
            ]
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
            _assert_delegate_worker_receipt(current, delegate_worker_marker)
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
                config=self.config if _remote_config_snapshot is None else _remote_config_snapshot,
            )
            if str(_binding_value(binding, "agent_revision_id", "")) != revision_id:
                raise CLIDevelopmentSessionConflict(
                    "Exact AgentRevision binding is unavailable",
                    code="agent_revision_binding_required",
                )
            bound_route = _assert_route(
                binding, provider=MUSE_PROVIDER, model=None, effort=None
            )
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

    async def create_session(self, request: Any = None, *, _remote_config_snapshot: Any = None, **kwargs: Any) -> dict[str, Any]:
        delegate_worker_marker = _typed_delegate_worker_context(
            kwargs.pop("_delegate_worker_context", None)
        )
        req = CreateCLIDevelopmentSessionRequest.from_value(request, **kwargs)
        worker_marker = delegate_worker_marker
        if worker_marker is not None:
            expected_provider = {
                "codex": "codex-cli",
                "antigravity": "antigravity-cli",
                "claude": "claude-cli",
                "grok": "grok-cli",
                "muse": MUSE_PROVIDER,
            }.get(str(worker_marker.get("kind") or ""))
            scope_value = worker_marker.get("workspace_scope")
            expected_session_key = f"delegate-worker:{worker_marker.get('dialogue_id')}"
            actual_session_key = str(req.idempotency_key)
            idempotency_key_matches = actual_session_key == expected_session_key or bool(
                re.fullmatch(
                    re.escape(expected_session_key) + r":resume:[1-9][0-9]{0,8}",
                    actual_session_key,
                )
            )
            if (
                req.owner_id != str(worker_marker.get("owner_id") or "")
                or req.agent_id != str(worker_marker.get("agent_id") or "")
                or str(req.agent_revision_id or "")
                != str(worker_marker.get("agent_revision_id") or "")
                or req.provider != expected_provider
                or not isinstance(scope_value, Mapping)
                or worker_marker.get("workspace_scope_sha256") != _json_hash(scope_value)
                or tuple(req.allowed_capabilities) != tuple(worker_marker.get("capabilities") or ())
                or not idempotency_key_matches
            ):
                raise CLIDevelopmentSessionInvalid(
                    "delegate worker context does not match the pinned session request",
                    code="delegate_worker_context_invalid",
                )
        wire_fingerprint = self._wire_create_fingerprint(req)
        if worker_marker is not None:
            wire_fingerprint = _json_hash(
                {"request_fingerprint": wire_fingerprint, "delegate_worker": worker_marker}
            )
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
                _assert_delegate_worker_receipt(current, delegate_worker_marker)
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
            _remote_config_snapshot = await self._resolve_remote_config_snapshot(_remote_config_snapshot)
            return await self._create_remote_session(
                req,
                session_id=session_id,
                wire_fingerprint=wire_fingerprint,
                delegate_worker_marker=delegate_worker_marker,
                _remote_config_snapshot=_remote_config_snapshot,
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
        delegate_worker_marker = _validate_delegate_worker_context(
            delegate_worker_marker,
            req,
            agent_id=agent_id,
            revision_id=revision_id,
        )
        if delegate_worker_marker is not None and effective_revision_id != revision_id:
            raise CLIDevelopmentSessionConflict(
                "managed worker sessions require an exact AgentRevision pin",
                code="delegate_worker_revision_pin_required",
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
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        except Exception as exc:
            raise CLIDevelopmentSessionConflict("Agent route is unavailable", code="agent_route_unavailable") from exc
        if (
            req.allowed_capabilities != CLI_DEVELOPMENT_CAPABILITIES
            and req.provider not in {"claude", "claude-cli", "claudecode"}
            and worker_marker is None
        ):
            raise CLIDevelopmentSessionInvalid("narrowed capabilities are only enforceable for the Claude provider", code="allowed_capabilities_unsupported_provider")
        authority_revision_id = effective_revision_id or revision_id
        authority = await self._resolve_authority(agent_id=agent_id, revision_id=authority_revision_id, project_id=req.project_id, capabilities=req.allowed_capabilities, managed_backend=delegate_worker_marker is not None)
        context_tuple = self._context_tuple(owner_id=req.owner_id, agent_id=agent_id, provider=req.provider, model=req.model, effort=req.effort, working_directory=cwd, idempotency_key=req.idempotency_key, project_id=req.project_id, permission_mode=permission_mode, title=req.title, allowed_capabilities=req.allowed_capabilities)
        if worker_marker is not None:
            context_tuple["delegate_worker"] = worker_marker
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
        if delegate_worker_marker is not None:
            context_tuple["delegate_worker"] = delegate_worker_marker
            deployment_material["delegate_worker_scope_sha256"] = delegate_worker_marker[
                "workspace_scope_sha256"
            ]
            context_tuple["deployment_fingerprint"] = _json_hash(deployment_material)
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
                _assert_delegate_worker_receipt(current, delegate_worker_marker)
                if str(getattr(existing, "user_id", "")) != req.owner_id or current.get("idempotency_digest") != context_tuple["idempotency_digest"] or not self._wire_create_fingerprint_matches(req, current, wire_fingerprint):
                    raise CLIDevelopmentSessionConflict("session idempotency key belongs to another request", code="session_idempotency_conflict")
                for fixed_key in ("agent_id", "provider", "model", "effort", "working_directory", "project_id", "permission_mode", "role_context_mode", "role_context_sha256"):
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
                )
                exact_route = _assert_route(
                    exact_binding,
                    provider=req.provider,
                    model=req.model,
                    effort=req.effort,
                )
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
                _assert_delegate_worker_receipt(current, delegate_worker_marker)
                if str(getattr(winner, "user_id", "")) != req.owner_id or current.get("idempotency_digest") != context_tuple["idempotency_digest"] or not self._wire_create_fingerprint_matches(req, current, wire_fingerprint):
                    raise CLIDevelopmentSessionConflict("session idempotency key conflict", code="session_idempotency_conflict")
                for fixed_key in ("agent_id", "provider", "model", "effort", "working_directory", "project_id", "permission_mode", "role_context_mode", "role_context_sha256"):
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
            )
        except Exception as exc:
            raise CLIDevelopmentSessionForbidden("Agent binding could not be persisted", code="agent_binding_denied") from exc
        binding = getattr(result, "binding", None)
        if binding is None:
            raise CLIDevelopmentSessionConflict("immutable Agent binding is missing", code="agent_binding_required")
        bound_route = _assert_route(binding, provider=req.provider, model=req.model, effort=req.effort)
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
        remote_projection: dict[str, Any] = {}
        if remote:
            authorized_scope_record = None
            summary = getattr(work, "result_summary_json", None)
            summary = summary if isinstance(summary, Mapping) else {}
            plan_receipt = summary.get("muse_tool_loop_scope")
            if isinstance(plan_receipt, Mapping) and plan_receipt.get("mode") == "authorized_scope":
                authorized_scope_record = await self._muse_authorized_scope_for_context(
                    db,
                    context=context,
                    owner_id=owner_id,
                    session_id=str(getattr(row, "id", "") or ""),
                    binding_id=str(context.get("binding_id") or ""),
                )
            remote_projection = {
                "turn_status": remote_fields.get("turn_status"),
                "delivery": remote_fields.get("delivery"),
                "history_observation": remote_fields.get("history_observation"),
                "remote_reconnect": _remote_reconnect_receipt_projection(context),
                "cli_failure": self._remote_cli_failure_projection(
                    row=row, run=run, work=work, context=context, owner_id=owner_id,
                ),
            }
            tool_loop_diagnostics = self._muse_tool_loop_diagnostics(
                row=row,
                run=run,
                work=work,
                context=context,
                owner_id=owner_id,
                authorized_scope_record=authorized_scope_record,
            )
            if tool_loop_diagnostics is not None:
                remote_projection["muse_tool_loop_diagnostics"] = tool_loop_diagnostics
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
        run_projection = run.to_dict() if run is not None else None
        work_projection = work.to_safe_dict() if work is not None else None
        if remote:
            run_projection = self._sanitize_cause_in_result_projection(
                run_projection, result_key="result",
            )
            work_projection = self._sanitize_cause_in_result_projection(
                work_projection, result_key="result_summary_json",
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
            **remote_projection,
            "status": self._status_from_rows(context, run, work),
            "created": created,
            "policy": policy_projection,
            "native_state": None if remote else context.get("native_state", "unknown"),
            "run": run_projection,
            "work": work_projection,
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
        delegate_worker_limits: Mapping[str, Any] | None = None,
        delegate_worker_audit: Mapping[str, Any] | None = None,
        borrowed: bool = False,
        trigger_message: ConversationMessage | None = None,
        _remote_config_snapshot: Any = None,
    ) -> dict[str, Any]:
        """Create a remote Work item while the session row is locked."""

        self._assert_remote_admission(
            owner_id=request.owner_id,
            agent_id=str(context.get("agent_id") or row.agent_id or ""),
            _remote_config_snapshot=_remote_config_snapshot,
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
        await self._resolve_remote_backend_execution(db, row, context, _remote_config_snapshot=_remote_config_snapshot)
        existing_message = (await db.execute(select(ConversationMessage).where(ConversationMessage.session_id == row.id, ConversationMessage.client_message_id == send_key).with_for_update())).scalars().first()
        if trigger_message is not None:
            if existing_message is not None and existing_message is not trigger_message:
                raise CLIDevelopmentSessionConflict("send key belongs to another message", code="send_idempotency_conflict")
            trigger_message.message_metadata = {**(trigger_message.message_metadata or {}), "cli_development": True, "execution_target": MUSE_EXECUTION_TARGET}
            trigger_message_id = trigger_message.id
        elif existing_message is not None:
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
        if delegate_worker_audit is not None:
            remote_run_metadata["delegate_worker"] = copy.deepcopy(
                dict(delegate_worker_audit)
            )
            manifest["delegate_worker"] = copy.deepcopy(
                dict(delegate_worker_audit)
            )
        if request.muse_tool_loop_opt_in:
            remote_run_metadata["muse_tool_loop_opt_in"] = True
        worker_session_marker = context.get("delegate_worker")
        if delegate_worker_limits is not None:
            if not isinstance(worker_session_marker, Mapping):
                raise CLIDevelopmentSessionForbidden(
                    "delegate worker session marker is missing",
                    code="delegate_worker_context_missing",
                )
            remote_run_metadata["delegate_worker_limits"] = dict(delegate_worker_limits)
        if role_pins is not None:
            remote_run_metadata["role_context"] = dict(role_pins)
        manifest_hash = _json_hash(manifest)
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
        if delegate_worker_audit is not None:
            work_metadata["delegate_worker"] = copy.deepcopy(
                dict(delegate_worker_audit)
            )
        if request.muse_tool_loop_opt_in:
            work_metadata["muse_tool_loop_opt_in"] = True
        if delegate_worker_limits is not None:
            work_metadata["delegate_worker_limits"] = dict(delegate_worker_limits)
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
        timeout_value = (
            delegate_worker_limits.get("timeout_seconds")
            if isinstance(delegate_worker_limits, Mapping)
            else None
        )
        work_deadline = (
            self._clock() + timedelta(seconds=int(timeout_value))
            if isinstance(timeout_value, int) and not isinstance(timeout_value, bool)
            else None
        )
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
            deadline=work_deadline,
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
        if borrowed:
            await db.flush()
        else:
            await db.commit()
        return {"accepted": True, "created": True, "session_id": sid, "run_id": str(run_id), "work_item_id": str(work.id), "status": "queued", "execution_target": MUSE_EXECUTION_TARGET, "local_filesystem": False, "sandbox": "remote_uncontrolled", "run": run.to_dict(), "work": work.to_safe_dict()}

    async def send(
        self, session_id: Any, request: Any = None, *, _db_session: Any = None,
        _trigger_message: ConversationMessage | None = None, _remote_config_snapshot: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Admit a turn; a borrowed session remains entirely owned by the caller."""
        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        delegate_worker_limits = _typed_delegate_worker_limits(
            kwargs.pop("_delegate_worker_limits", None)
        )
        worker_limits_marker = delegate_worker_limits
        req = SendCLIDevelopmentSessionRequest.from_value(request, **kwargs)
        borrowed = _db_session is not None
        if _trigger_message is not None and not borrowed:
            raise CLIDevelopmentSessionInvalid("trigger message requires a borrowed session", code="invalid_trigger_message")
        db = _db_session if borrowed else await self._session()
        try:
            row = (await db.execute(select(ConversationSession).where(ConversationSession.id == uuid.UUID(sid), ConversationSession.deleted_at.is_(None)).with_for_update())).scalars().first()
            if row is None:
                raise CLIDevelopmentSessionNotFound()
            if str(row.user_id) != req.owner_id:
                raise CLIDevelopmentSessionForbidden("session owner mismatch", code="session_owner_mismatch")
            if _trigger_message is not None:
                if (
                    not isinstance(_trigger_message, ConversationMessage)
                    or object_session(_trigger_message) is not db.sync_session
                    or _trigger_message in db.sync_session.deleted
                    or _trigger_message.session_id != row.id
                    or _trigger_message.content != req.content
                    or _trigger_message.id is None
                    or not isinstance(_trigger_message.message_metadata, Mapping)
                ):
                    raise CLIDevelopmentSessionInvalid("invalid trigger message", code="invalid_trigger_message")
                await db.flush()
            context = self._context(row)
            if not context or str(context.get("schema_version")) != str(CLI_DEVELOPMENT_SCHEMA_VERSION):
                raise CLIDevelopmentSessionConflict("CLI session context is invalid", code="cli_session_context_invalid")
            raw_worker_marker = context.get("delegate_worker")
            if raw_worker_marker is not None and not isinstance(raw_worker_marker, Mapping):
                raise CLIDevelopmentSessionUnavailable(
                    "managed worker session marker is invalid",
                    code="delegate_worker_context_invalid",
                )
            delegate_worker_audit = _validate_delegate_worker_limits(
                raw_worker_marker if isinstance(raw_worker_marker, Mapping) else None,
                delegate_worker_limits,
            )
            if raw_worker_marker is not None and str(raw_worker_marker.get("owner_id") or "") != req.owner_id:
                raise CLIDevelopmentSessionForbidden(
                    "managed worker owner mismatch",
                    code="delegate_worker_owner_mismatch",
                )
            worker_context = context.get("delegate_worker")
            if worker_limits_marker is not None:
                if not isinstance(worker_context, Mapping):
                    raise CLIDevelopmentSessionConflict(
                        "delegate worker session marker is missing",
                        code="delegate_worker_context_missing",
                    )
                if (
                    worker_limits_marker.get("schema_version") != 1
                    or str(worker_limits_marker.get("dialogue_id") or "")
                    != str(worker_context.get("dialogue_id") or "")
                    or str(worker_limits_marker.get("workspace_scope_sha256") or "")
                    != str(worker_context.get("workspace_scope_sha256") or "")
                ):
                    raise CLIDevelopmentSessionConflict(
                        "delegate worker limits differ from its prepared session",
                        code="delegate_worker_limits_mismatch",
                    )
                from ..memory.models import AgentDialogue

                worker_dialogue = await db.get(
                    AgentDialogue,
                    uuid.UUID(str(worker_limits_marker["dialogue_id"])),
                )
                auth = (
                    worker_dialogue.authorization_json
                    if worker_dialogue is not None
                    and isinstance(worker_dialogue.authorization_json, Mapping)
                    else {}
                )
                authorized_worker = auth.get("delegate_worker")
                if (
                    worker_dialogue is None
                    or str(worker_dialogue.worker_session_id) != sid
                    or str(worker_dialogue.owner_user_id) != req.owner_id
                    or not isinstance(authorized_worker, Mapping)
                    or dict(authorized_worker.get("workspace_scope") or {})
                    != dict(worker_context.get("workspace_scope") or {})
                    or str(authorized_worker.get("workspace_scope_sha256") or "")
                    != str(worker_context.get("workspace_scope_sha256") or "")
                    or str(authorized_worker.get("kind") or "")
                    != str(worker_context.get("kind") or "")
                ):
                    raise CLIDevelopmentSessionForbidden(
                        "delegate worker limits do not match the current Dialogue",
                        code="delegate_worker_dialogue_binding_invalid",
                    )
                authorized_budget = (
                    authorized_worker.get("budgets")
                    if isinstance(authorized_worker.get("budgets"), Mapping)
                    else auth.get("budget")
                )
                aggregate_budget = auth.get("budget")
                if isinstance(authorized_budget, Mapping):
                    supplied_budgets = worker_limits_marker.get("budgets") or {}
                    for key in ("max_tokens", "max_cost_micros", "max_attempts"):
                        expected = authorized_budget.get(key)
                        supplied = supplied_budgets.get(key) if isinstance(supplied_budgets, Mapping) else None
                        if expected is not None and int(expected) != int(supplied or 0):
                            raise CLIDevelopmentSessionForbidden(
                                "delegate worker budget differs from its session reservation",
                                code="delegate_worker_budget_mismatch",
                            )
                        aggregate = aggregate_budget.get(key) if isinstance(aggregate_budget, Mapping) else None
                        if expected is not None and aggregate is not None and int(expected) > int(aggregate):
                            raise CLIDevelopmentSessionForbidden(
                                "delegate worker budget exceeds its Dialogue ceiling",
                                code="delegate_worker_budget_exceeds_dialogue",
                            )
            if req.muse_tool_loop_opt_in and not self._is_remote_context(context):
                raise CLIDevelopmentSessionConflict(
                    "Muse scoped tool loop is only available on muse-remote sessions",
                    code="muse_tool_loop_provider_mismatch",
                )
            # Deduplication is the first send decision after owner/context
            # validation. A completed/failed receipt with the same key must
            # be replayable; state checks apply only to a new key.
            send_key = req.send_key  # preserve exact identity; no strip
            key_digest = _sha256(send_key)
            request_fingerprint = _sha256(
                req.content
                if not req.muse_tool_loop_opt_in
                else "muse-scoped-tool-loop-v1\x00" + req.content
            )
            if worker_limits_marker is not None:
                request_fingerprint = _json_hash(
                    {
                        "request_fingerprint": request_fingerprint,
                        "delegate_worker_limits": worker_limits_marker,
                    }
                )
            existing = (await db.execute(select(AgentRun).where(AgentRun.session_id == row.id, AgentRun.user_id == req.owner_id, AgentRun.client_message_key == key_digest).with_for_update())).scalars().first()
            if existing is not None:
                if _trigger_message is not None and existing.trigger_message_id != _trigger_message.id:
                    raise CLIDevelopmentSessionConflict("send key belongs to another message", code="send_idempotency_conflict")
                if existing.request_fingerprint != request_fingerprint:
                    raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
                return {"accepted": True, "created": False, "session_id": sid, "run": existing.to_dict(), "run_id": str(existing.id), "status": self._status_from_rows(context, existing, await db.get(AgentWorkItem, existing.work_item_id) if existing.work_item_id else None)}
            if self._is_remote_context(context):
                _remote_config_snapshot = await self._resolve_remote_config_snapshot(_remote_config_snapshot)
                return await self._send_remote_locked(
                    db,
                    row,
                    context,
                    req,
                    sid=sid,
                    send_key=send_key,
                    key_digest=key_digest,
                    request_fingerprint=request_fingerprint,
                    delegate_worker_limits=worker_limits_marker,
                    delegate_worker_audit=delegate_worker_audit,
                    borrowed=borrowed,
                    trigger_message=_trigger_message,
                    _remote_config_snapshot=_remote_config_snapshot,
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
            await self._resolve_authority(agent_id=str(context.get("agent_id") or row.agent_id), revision_id=str(context.get("agent_revision_id") or ""), project_id=context.get("project_id"), capabilities=pinned_caps, managed_backend=await self._verified_managed_backend(db, row, context))
            existing_message = (await db.execute(select(ConversationMessage).where(ConversationMessage.session_id == row.id, ConversationMessage.client_message_id == send_key).with_for_update())).scalars().first()
            if _trigger_message is not None:
                if existing_message is not None and existing_message is not _trigger_message:
                    raise CLIDevelopmentSessionConflict("send key belongs to another message", code="send_idempotency_conflict")
                _trigger_message.message_metadata = {**_trigger_message.message_metadata, "cli_development": True}
                trigger_message_id = _trigger_message.id
            elif existing_message is not None:
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
            if delegate_worker_audit is not None:
                manifest["delegate_worker"] = copy.deepcopy(
                    dict(delegate_worker_audit)
                )
            if role_context_pins is not None:
                manifest["role_context"] = dict(role_context_pins)
            manifest_hash = _json_hash(manifest)
            cli_run_metadata = {"schema_version": CLI_DEVELOPMENT_SCHEMA_VERSION, "send_key_digest": key_digest, "effort": str(context.get("effort") or ""), "permission_mode": str(context.get("permission_mode") or "native_default"), "approval_policy_source": "provider_native_default_unknown" if str(context.get("permission_mode") or "native_default") == "native_default" else "explicit_deployment_policy", "execution_lane": policy.execution_lane, "working_directory_fingerprint": _sha256(cwd), "provider_cache_root_fingerprint": _sha256(str(context.get("provider_cache_root") or "")), "binding_id": context.get("binding_id"), "binding_snapshot_hash": context.get("binding_snapshot_hash")}
            if worker_limits_marker is not None:
                cli_run_metadata["delegate_worker_limits"] = dict(worker_limits_marker)
            if delegate_worker_audit is not None:
                cli_run_metadata["delegate_worker"] = copy.deepcopy(
                    dict(delegate_worker_audit)
                )
            if role_context_pins is not None:
                cli_run_metadata["role_context"] = dict(role_context_pins)
            run = AgentRun(id=run_id, root_run_id=run_id, session_id=row.id, trigger_message_id=trigger_message_id, agent_id=uuid.UUID(str(context.get("agent_id") or row.agent_id)), agent_revision_id=uuid.UUID(str(context.get("agent_revision_id"))), user_id=req.owner_id, client_message_id=send_key, client_message_key=key_digest, request_fingerprint=request_fingerprint, run_type=CLI_SESSION_SOURCE_TYPE, status="queued", provider=str(context.get("provider") or ""), model=str(context.get("model") or ""), generation_profile="assisted_work", work_item_attempt=1, objective="", title="", result={}, validation={}, run_metadata={"cli_development": cli_run_metadata}, resolved_execution_manifest=manifest, execution_manifest_hash=manifest_hash, created_at=self._clock(), updated_at=self._clock(), last_event_at=self._clock())
            db.add(run)
            await db.flush()
            resource_key = f"conversation:{row.id}"
            work_metadata = {"owner_id": req.owner_id, "send_key_digest": key_digest, "provider": run.provider, "model": run.model, "effort": str(context.get("effort") or ""), "permission_mode": str(context.get("permission_mode") or "native_default"), "execution_lane": policy.execution_lane, "binding_id": context.get("binding_id"), "binding_snapshot_hash": context.get("binding_snapshot_hash")}
            if worker_limits_marker is not None:
                work_metadata["delegate_worker_limits"] = dict(worker_limits_marker)
            timeout_value = (
                worker_limits_marker.get("timeout_seconds")
                if isinstance(worker_limits_marker, Mapping)
                else None
            )
            work_deadline = (
                self._clock() + timedelta(seconds=int(timeout_value))
                if isinstance(timeout_value, int) and not isinstance(timeout_value, bool)
                else None
            )
            if delegate_worker_audit is not None:
                work_metadata["delegate_worker"] = copy.deepcopy(
                    dict(delegate_worker_audit)
                )
            if role_context_pins is not None:
                work_metadata["role_context"] = dict(role_context_pins)
            work =AgentWorkItem(source_type=CLI_SESSION_SOURCE_TYPE, source_id=str(run_id), source_revision=str(context.get("agent_revision_id") or ""), intent_key=CLI_SESSION_ADAPTER_KEY, domain=CLI_DEVELOPMENT_DOMAIN, conversation_session_id=row.id, assigned_agent_id=run.agent_id, agent_revision_id=run.agent_revision_id, required_capabilities_json=list(pinned_caps), execution_adapter=CLI_SESSION_ADAPTER_KEY, max_attempts=1, deadline=work_deadline, state="pending", attempt_count=0, concurrency_key=resource_key, metadata_json=work_metadata, created_at=self._clock(), updated_at=self._clock())
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
            if borrowed:
                await db.flush()
            else:
                await db.commit()
            return {"accepted": True, "created": True, "session_id": sid, "run_id": str(run_id), "work_item_id": str(work.id), "status": "queued", "run": run.to_dict(), "work": work.to_safe_dict()}
        except IntegrityError as exc:
            if borrowed:
                raise
            await self._rollback_async(db)
            if not _is_unique_integrity_error(exc):
                raise CLIDevelopmentSessionStorageError("CLI send could not be persisted", code="send_storage_error") from None
            probe = await self._session()
            try:
                winner = (await probe.execute(select(AgentRun).where(AgentRun.session_id == uuid.UUID(sid), AgentRun.user_id == req.owner_id, AgentRun.client_message_key == _sha256(req.send_key)).limit(1))).scalars().first()
                if winner is None:
                    raise CLIDevelopmentSessionConflict("send idempotency race could not be reconciled", code="send_idempotency_unavailable")
                if winner.request_fingerprint != request_fingerprint:
                    raise CLIDevelopmentSessionConflict("send key was reused with different raw content", code="send_idempotency_conflict")
                return {"accepted": True, "created": False, "session_id": sid, "run_id": str(winner.id), "status": "queued", "run": winner.to_dict()}
            finally:
                await self._close(probe)
        finally:
            if not borrowed:
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

    async def _reconcile_remote_history_on_reconnect(
        self, session_id: str, *, owner_id: str, _remote_config_snapshot: Any = None
    ) -> dict[str, Any]:
        """Read provider history for the exact uncertain Muse turn and settle it once."""

        snapshot = await self._resolve_remote_config_snapshot(_remote_config_snapshot)
        lock = await _remote_history_reconciliation_lock(session_id)
        async with lock:
            db = await self._session()
            try:
                row = await db.get(ConversationSession, uuid.UUID(session_id))
                if (
                    row is None
                    or row.deleted_at is not None
                    or str(row.user_id) != owner_id
                ):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "binding_mismatch",
                    }
                context = self._context(row)
                if not self._is_remote_context(context):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "binding_mismatch",
                    }
                run, work = await self._load_latest(db, row.id)
                raw_remote = (
                    row.context.get(MUSE_REMOTE_CONTEXT_KEY)
                    if isinstance(row.context, Mapping)
                    else None
                )
                raw_binding = (
                    raw_remote.get("binding")
                    if isinstance(raw_remote, Mapping)
                    and isinstance(raw_remote.get("binding"), Mapping)
                    else raw_remote
                )
                if not isinstance(raw_binding, Mapping):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "binding_mismatch",
                    }

                run_result = (
                    run.result if isinstance(getattr(run, "result", None), Mapping) else {}
                )
                work_result = (
                    work.result_summary_json
                    if isinstance(getattr(work, "result_summary_json", None), Mapping)
                    else {}
                )
                work_metadata = (
                    work.metadata_json
                    if isinstance(getattr(work, "metadata_json", None), Mapping)
                    else {}
                )
                run_metadata = (
                    run.run_metadata
                    if isinstance(getattr(run, "run_metadata", None), Mapping)
                    else {}
                )
                run_cli_metadata = (
                    run_metadata.get("cli_development")
                    if isinstance(run_metadata.get("cli_development"), Mapping)
                    else {}
                )
                tool_loop_opt_in = _matching_muse_tool_loop_opt_in(
                    work_metadata, run_cli_metadata
                )
                if tool_loop_opt_in is None:
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "tool_loop_metadata_mismatch",
                    }
                if (
                    run is not None
                    and work is not None
                    and str(run.status or "") == "succeeded"
                    and str(work.state or "") == "succeeded"
                    and run_result.get("remote_history_reconciled") is True
                    and work_result.get("remote_history_reconciled") is True
                    and not raw_binding.get("current_turn_nonce")
                    and not raw_binding.get("current_run_id")
                    and str(raw_binding.get("state") or "") == "bound"
                    and raw_binding.get("safe_to_send") is True
                ):
                    durable_message = (
                        await db.execute(
                            select(ConversationMessage).where(
                                ConversationMessage.session_id == row.id,
                                ConversationMessage.client_message_id
                                == f"muse-assistant:{run.id}",
                            )
                        )
                    ).scalars().first()
                    if (
                        durable_message is not None
                        and str(run_result.get("assistant_message_id") or "")
                        == str(durable_message.id)
                        and str(
                            run_result.get("provider_assistant_message_id") or ""
                        )
                        == str(
                            (durable_message.message_metadata or {}).get(
                                "provider_message_id"
                            )
                            or ""
                        )
                    ):
                        return {
                            "reconciled": True,
                            "history_reconciled": True,
                            "reconciliation_diagnostic": "confirmed",
                        }
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "settlement_conflict",
                    }

                raw_nonce = raw_binding.get("current_turn_nonce")
                if raw_nonce is not None and not isinstance(raw_nonce, str):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "recovery_state_ineligible",
                    }
                nonce = str(raw_nonce or "").strip()
                if not nonce:
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "no_pending_nonce",
                    }

                if run is None or work is None:
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "recovery_state_ineligible",
                    }
                agent_id = str(context.get("agent_id") or row.agent_id or "")
                revision_id = str(context.get("agent_revision_id") or "")
                expected_binding_id = str(context.get("binding_id") or "")
                expected_binding_hash = str(context.get("binding_snapshot_hash") or "")
                attempt = int(getattr(work, "attempt_count", 0) or 0)
                recovery_error_code = str(run.error or "")
                work_event = (
                    await db.execute(
                        select(AgentWorkEvent)
                        .where(
                            AgentWorkEvent.work_item_id == work.id,
                            AgentWorkEvent.event_type == "work.uncertain",
                            AgentWorkEvent.to_state == "uncertain",
                            AgentWorkEvent.agent_run_id == run.id,
                            AgentWorkEvent.safe_error_code.in_(
                                (
                                    str(MuseErrorCode.SEND_RESPONSE_UNCORRELATED),
                                    _MUSE_CLI_PROCESS_FAILED,
                                )
                            ),
                        )
                        .order_by(desc(AgentWorkEvent.sequence))
                        .limit(1)
                    )
                ).scalars().first()
                run_event = (
                    await db.execute(
                        select(AgentRunEvent)
                        .where(
                            AgentRunEvent.run_id == run.id,
                            AgentRunEvent.event_type == "cli.disconnected",
                            AgentRunEvent.status == "disconnected",
                        )
                        .order_by(desc(AgentRunEvent.sequence))
                        .limit(1)
                    )
                ).scalars().first()
                run_event_payload = (
                    run_event.payload
                    if isinstance(getattr(run_event, "payload", None), Mapping)
                    else {}
                )
                binding_identity_matches = (
                    str(raw_binding.get("owner_id") or "") == owner_id
                    and str(raw_binding.get("agent_id") or "") == agent_id
                    and str(raw_binding.get("revision_id") or "") == revision_id
                    and str(raw_binding.get("remote_target_key") or "") == session_id
                    and str(raw_binding.get("current_run_id") or "") == str(run.id)
                )
                recovery_state_matches = (
                    str(run.status or "") == "failed"
                    and str(work.state or "") == "uncertain"
                    and str(work.outcome_classification or "") == "uncertain"
                    and _remote_recovery_error_matches(
                        run_error=run.error,
                        work_error=work.safe_error_code,
                        binding=raw_binding,
                        result_summary=work_result,
                    )
                    and attempt > 0
                    and int(getattr(run, "work_item_attempt", 0) or 0) == attempt
                    and getattr(work, "active_agent_run_id", None)
                    in (None, run.id)
                    and str(run.work_item_id or "") == str(work.id)
                    and str(work.conversation_session_id or "") == session_id
                    and str(run.session_id or "") == session_id
                    and str(run.user_id or "") == owner_id
                    and str(run.agent_id or "") == agent_id
                    and str(run.agent_revision_id or "") == revision_id
                    and str(work.assigned_agent_id or "") == agent_id
                    and str(work.agent_revision_id or "") == revision_id
                    and str(run.run_type or "") == REMOTE_SESSION_SOURCE_TYPE
                    and str(work.source_type or "") == REMOTE_SESSION_SOURCE_TYPE
                    and str(work.execution_adapter or "") == MUSE_REMOTE_ADAPTER_KEY
                    and str(work.source_id or "") == str(run.id)
                    and str(work.source_revision or "") == revision_id
                    and work_metadata.get("owner_id") == owner_id
                    and work_metadata.get("remote_target_key") == session_id
                    and str(work_metadata.get("binding_id") or "")
                    == expected_binding_id
                    and str(work_metadata.get("binding_snapshot_hash") or "")
                    == expected_binding_hash
                    and str(run_cli_metadata.get("binding_id") or "")
                    == expected_binding_id
                    and str(run_cli_metadata.get("binding_snapshot_hash") or "")
                    == expected_binding_hash
                    and str(run_cli_metadata.get("remote_target_key") or "")
                    == session_id
                    and binding_identity_matches
                    and work_event is not None
                    and _remote_uncertain_event_matches(
                        work_event,
                        run_id=run.id,
                        attempt=attempt,
                        error_code=recovery_error_code,
                    )
                    and run_event is not None
                    and str(run_event_payload.get("error_code") or "")
                    == recovery_error_code
                )
                latest_run, latest_work = await self._load_latest(db, row.id)
                if (
                    not recovery_state_matches
                    or latest_run is None
                    or latest_work is None
                    or str(latest_run.id) != str(run.id)
                    or str(latest_work.id) != str(work.id)
                ):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "recovery_state_ineligible",
                    }

                self._assert_remote_admission(owner_id=owner_id, agent_id=agent_id, _remote_config_snapshot=snapshot)
                binding = await self._binding_for_row(db, row)
                _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
                role_mapping = self._managed_role_mapping(binding, context)
                await self._resolve_remote_backend_execution(db, row, context, _remote_config_snapshot=snapshot)
                if tool_loop_opt_in:
                    existing_observation = run_result.get(
                        "muse_tool_loop_history_observation"
                    )
                    if existing_observation is not None:
                        observation_matches = bool(
                            isinstance(existing_observation, Mapping)
                            and existing_observation.get("schema_version") == 1
                            and existing_observation.get("status")
                            == "tool_loop_recovery_required"
                            and existing_observation.get("run_id") == str(run.id)
                            and existing_observation.get("work_item_id")
                            == str(work.id)
                            and existing_observation.get("binding_id")
                            == expected_binding_id
                            and existing_observation.get("binding_snapshot_hash")
                            == expected_binding_hash
                            and existing_observation.get("turn_nonce_sha256")
                            == _sha256(nonce)
                            and isinstance(
                                existing_observation.get(
                                    "provider_assistant_message_id"
                                ),
                                str,
                            )
                            and isinstance(
                                existing_observation.get("assistant_text_sha256"),
                                str,
                            )
                        )
                        if not observation_matches:
                            return {
                                "reconciled": False,
                                "history_reconciled": False,
                                "reconciliation_diagnostic": "history_observation_conflict",
                            }
                        return {
                            "reconciled": False,
                            "history_reconciled": False,
                            "history_observed": True,
                            "reconciliation_diagnostic": "tool_loop_recovery_required",
                            "history_observation": dict(existing_observation),
                        }
                metadata: dict[str, Any] = {
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "agent_revision_id": revision_id,
                    "execution_target": MUSE_EXECUTION_TARGET,
                    "provider": MUSE_PROVIDER,
                    "remote_target_key": session_id,
                    "current_run_id": str(run.id),
                }
                if role_mapping is not None:
                    metadata["managed_role_context"] = role_mapping
                claim = {
                    "work_item_id": str(work.id),
                    "source_type": REMOTE_SESSION_SOURCE_TYPE,
                    "source_id": str(run.id),
                    "source_revision": revision_id,
                    "intent_key": str(work.intent_key or MUSE_REMOTE_ADAPTER_KEY),
                    "domain": str(work.domain or CLI_DEVELOPMENT_DOMAIN),
                    "assigned_agent_id": agent_id,
                    "agent_revision_id": revision_id,
                    "execution_adapter": MUSE_REMOTE_ADAPTER_KEY,
                    "required_capabilities": list(REMOTE_SESSION_CAPABILITIES),
                    "conversation_session_id": session_id,
                    "metadata": metadata,
                }
                authority = {
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "revision_id": revision_id,
                    "remote_target_key": session_id,
                    "required_capabilities": list(REMOTE_SESSION_CAPABILITIES),
                    "allowed_adapters": [MUSE_REMOTE_ADAPTER_KEY],
                    "allowed_targets": [MUSE_EXECUTION_TARGET],
                }
                recovery_nonce = nonce
                run_id = str(run.id)
                work_id = str(work.id)
                await self._close(db)
                db = None
            except Exception:
                return {
                    "reconciled": False,
                    "history_reconciled": False,
                    "reconciliation_diagnostic": "authority_unavailable",
                }
            finally:
                if db is not None:
                    await self._close(db)

            try:
                adapter = self._get_remote_adapter(_remote_config_snapshot=snapshot)
                reconcile = getattr(adapter, "reconcile", None)
                if not callable(reconcile):
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "reconciliation_diagnostic": "history_unavailable",
                    }
                outcome = await reconcile(
                    claim,
                    authority=authority,
                    preserve_binding=True,
                )
            except Exception:
                return {
                    "reconciled": False,
                    "history_reconciled": False,
                    "reconciliation_diagnostic": "history_unavailable",
                }
            outcome_metadata = (
                outcome.metadata
                if isinstance(getattr(outcome, "metadata", None), Mapping)
                else {}
            )
            diagnostic = str(
                outcome_metadata.get("reconciliation_diagnostic") or ""
            )
            allowed_diagnostics = {
                "confirmed",
                "history_unavailable",
                "history_nonterminal",
                "history_unconfirmed",
                "binding_mismatch",
                "authority_unavailable",
                "binding_persist_failed",
                "recovery_state_ineligible",
                "no_pending_nonce",
            }
            if diagnostic not in allowed_diagnostics:
                code = str(getattr(outcome, "error_code", "") or "")
                diagnostic = (
                    "authority_unavailable"
                    if code
                    in {
                        str(MuseErrorCode.CURRENT_AUTH_MISMATCH),
                        str(MuseErrorCode.CREDENTIAL_UNAVAILABLE),
                        str(MuseErrorCode.REMOTE_AUTHENTICATION_FAILED),
                        str(MuseErrorCode.REMOTE_TARGET_DISABLED),
                        str(MuseErrorCode.REMOTE_VM_UNBOUND),
                    }
                    else "history_unconfirmed"
                )
            result = (
                outcome.result
                if isinstance(getattr(outcome, "result", None), Mapping)
                else {}
            )
            history_confirmed = bool(
                str(getattr(outcome, "classification", "")) == "succeeded"
                and outcome_metadata.get("history_reconciled") is True
                and diagnostic == "confirmed"
                and isinstance(result.get("assistant_text"), str)
                and bool(result.get("assistant_text"))
                and isinstance(result.get("assistant_message_id"), str)
                and bool(result.get("assistant_message_id"))
            )
            if not history_confirmed:
                return {
                    "reconciled": False,
                    "history_reconciled": False,
                    "reconciliation_diagnostic": diagnostic,
                }
            if tool_loop_opt_in:
                observation = await self._record_muse_tool_loop_history_observation(
                    session_id=session_id,
                    run_id=run_id,
                    work_item_id=work_id,
                    owner_id=owner_id,
                    recovery_nonce=recovery_nonce,
                    binding_id=expected_binding_id,
                    binding_snapshot_hash=expected_binding_hash,
                    _remote_config_snapshot=snapshot,
                    assistant_text=str(result.get("assistant_text") or ""),
                    provider_message_id=str(
                        result.get("assistant_message_id") or ""
                    ),
                )
                if observation.get("recorded") is not True:
                    return {
                        "reconciled": False,
                        "history_reconciled": False,
                        "history_observed": False,
                        "reconciliation_diagnostic": str(
                            observation.get("reconciliation_diagnostic")
                            or "history_observation_unavailable"
                        ),
                    }
                return {
                    "reconciled": False,
                    "history_reconciled": False,
                    "history_observed": True,
                    "reconciliation_diagnostic": "tool_loop_recovery_required",
                    "history_observation": observation["history_observation"],
                }
            delivery = result.get("delivery")
            try:
                receipt = await self.record_remote_reconciliation_result(
                    session_id=session_id,
                    run_id=run_id,
                    work_item_id=work_id,
                    owner_id=owner_id,
                    recovery_nonce=recovery_nonce,
                    assistant_text=str(result.get("assistant_text") or ""),
                    assistant_message_id=str(
                        result.get("assistant_message_id") or ""
                    ),
                    assistant_text_truncated=(
                        result.get("assistant_text_truncated") is True
                    ),
                    assistant_text_original_length=(
                        result.get("assistant_text_original_length")
                    ),
                    delivery=delivery if isinstance(delivery, Mapping) else None,
                )
            except Exception:
                return {
                    "reconciled": False,
                    "history_reconciled": True,
                    "reconciliation_diagnostic": "settlement_unavailable",
                }
            if receipt.get("discarded"):
                return {
                    "reconciled": False,
                    "history_reconciled": True,
                    "reconciliation_diagnostic": "settlement_conflict",
                }
            return {
                "reconciled": True,
                "history_reconciled": True,
                "reconciliation_diagnostic": "confirmed",
            }

    async def _record_muse_tool_loop_history_observation(
        self,
        *,
        session_id: str,
        run_id: str,
        work_item_id: str,
        owner_id: str,
        recovery_nonce: str,
        binding_id: str,
        binding_snapshot_hash: str,
        assistant_text: str,
        provider_message_id: str,
        _remote_config_snapshot: Any = None,
    ) -> dict[str, Any]:
        """Persist a correlated reply receipt without settling opted-in Work."""

        snapshot = await self._resolve_remote_config_snapshot(_remote_config_snapshot)
        db = await self._session()
        try:
            row = (
                await db.execute(
                    select(ConversationSession)
                    .where(ConversationSession.id == uuid.UUID(session_id))
                    .with_for_update()
                )
            ).scalars().first()
            run = await db.get(AgentRun, uuid.UUID(run_id))
            work = await db.get(AgentWorkItem, uuid.UUID(work_item_id))
            if (
                row is None
                or row.deleted_at is not None
                or str(row.user_id) != owner_id
                or run is None
                or work is None
                or str(run.user_id or "") != owner_id
                or str(run.session_id or "") != session_id
                or str(run.id) != run_id
                or str(run.work_item_id or "") != work_item_id
                or str(run.run_type or "") != REMOTE_SESSION_SOURCE_TYPE
                or str(work.id) != work_item_id
                or str(work.conversation_session_id or "") != session_id
                or str(work.source_type or "") != REMOTE_SESSION_SOURCE_TYPE
                or str(work.source_id or "") != run_id
                or str(work.execution_adapter or "") != MUSE_REMOTE_ADAPTER_KEY
                or str(run.status or "") != "failed"
                or str(work.state or "") != "uncertain"
                or str(work.outcome_classification or "") != "uncertain"
                or work.cancelled_at is not None
                or getattr(work, "active_agent_run_id", None) not in (None, run.id)
            ):
                return {
                    "recorded": False,
                    "reconciliation_diagnostic": "recovery_state_ineligible",
                }

            latest_run, latest_work = await self._load_latest(db, row.id)
            context = self._context(row)
            raw_remote = (
                row.context.get(MUSE_REMOTE_CONTEXT_KEY)
                if isinstance(row.context, Mapping)
                else None
            )
            raw_binding = (
                raw_remote.get("binding")
                if isinstance(raw_remote, Mapping)
                and isinstance(raw_remote.get("binding"), Mapping)
                else raw_remote
            )
            work_metadata = (
                work.metadata_json
                if isinstance(work.metadata_json, Mapping)
                else {}
            )
            run_metadata = (
                run.run_metadata
                if isinstance(run.run_metadata, Mapping)
                else {}
            )
            run_cli_metadata = (
                run_metadata.get("cli_development")
                if isinstance(run_metadata.get("cli_development"), Mapping)
                else {}
            )
            if (
                latest_run is None
                or latest_work is None
                or str(latest_run.id) != run_id
                or str(latest_work.id) != work_item_id
                or _matching_muse_tool_loop_opt_in(work_metadata, run_cli_metadata)
                is not True
                or not isinstance(raw_binding, Mapping)
                or str(raw_binding.get("current_turn_nonce") or "")
                != recovery_nonce
                or str(raw_binding.get("current_run_id") or "") != run_id
                or str(raw_binding.get("owner_id") or "") != owner_id
                or str(raw_binding.get("agent_id") or "")
                != str(context.get("agent_id") or row.agent_id or "")
                or str(raw_binding.get("revision_id") or "")
                != str(context.get("agent_revision_id") or "")
                or str(raw_binding.get("remote_target_key") or "") != session_id
                or str(run.agent_id or "")
                != str(context.get("agent_id") or row.agent_id or "")
                or str(run.agent_revision_id or "")
                != str(context.get("agent_revision_id") or "")
                or str(work.assigned_agent_id or "")
                != str(context.get("agent_id") or row.agent_id or "")
                or str(work.agent_revision_id or "")
                != str(context.get("agent_revision_id") or "")
                or str(work.source_revision or "")
                != str(context.get("agent_revision_id") or "")
                or int(getattr(work, "attempt_count", 0) or 0) <= 0
                or int(getattr(run, "work_item_attempt", 0) or 0)
                != int(getattr(work, "attempt_count", 0) or 0)
                or not _remote_recovery_error_matches(
                    run_error=run.error,
                    work_error=work.safe_error_code,
                    binding=raw_binding,
                    result_summary=work.result_summary_json,
                )
                or str(context.get("binding_id") or "") != binding_id
                or str(context.get("binding_snapshot_hash") or "")
                != binding_snapshot_hash
                or str(work_metadata.get("owner_id") or "") != owner_id
                or str(work_metadata.get("remote_target_key") or "")
                != session_id
                or str(work_metadata.get("binding_id") or "") != binding_id
                or str(work_metadata.get("binding_snapshot_hash") or "")
                != binding_snapshot_hash
                or str(run_cli_metadata.get("binding_id") or "") != binding_id
                or str(run_cli_metadata.get("binding_snapshot_hash") or "")
                != binding_snapshot_hash
                or str(run_cli_metadata.get("remote_target_key") or "")
                != session_id
            ):
                return {
                    "recorded": False,
                    "reconciliation_diagnostic": "binding_mismatch",
                }

            agent_id = str(context.get("agent_id") or row.agent_id or "")
            revision_id = str(context.get("agent_revision_id") or "")
            self._assert_remote_admission(owner_id=owner_id, agent_id=agent_id, _remote_config_snapshot=snapshot)
            binding = await self._binding_for_row(db, row)
            _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
            await self._resolve_remote_backend_execution(db, row, context, _remote_config_snapshot=snapshot)

            receipt_identity = {
                "schema_version": 1,
                "status": "tool_loop_recovery_required",
                "run_id": run_id,
                "work_item_id": work_item_id,
                "binding_id": binding_id,
                "binding_snapshot_hash": binding_snapshot_hash,
                "turn_nonce_sha256": _sha256(recovery_nonce),
                "provider_assistant_message_id": provider_message_id,
                "assistant_text_sha256": _sha256(assistant_text),
                "assistant_text_length": len(assistant_text),
                "tool_dispatch": "not_attempted",
                "tool_receipts": "not_modified",
                "work_budget": "original_deadline_preserved",
            }
            run_result = run.result if isinstance(run.result, Mapping) else {}
            existing = run_result.get("muse_tool_loop_history_observation")
            if isinstance(existing, Mapping):
                if any(existing.get(key) != value for key, value in receipt_identity.items()):
                    return {
                        "recorded": False,
                        "reconciliation_diagnostic": "history_observation_conflict",
                    }
                return {
                    "recorded": True,
                    "history_observation": dict(existing),
                }

            observed_at = self._clock()
            receipt = {
                **receipt_identity,
                "observed_at": (
                    observed_at.isoformat()
                    if hasattr(observed_at, "isoformat")
                    else str(observed_at)
                ),
            }
            run.result = {
                **dict(run_result),
                "muse_tool_loop_history_observation": receipt,
            }
            run.updated_at = observed_at
            await db.commit()
            return {"recorded": True, "history_observation": receipt}
        except Exception:
            await self._rollback_async(db)
            return {
                "recorded": False,
                "reconciliation_diagnostic": "authority_unavailable",
            }
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
                # Close the read session before the bounded provider history
                # read and its independent row-locked settlement transaction.
                await self._close(db)
                db = None
                snapshot = await self._resolve_remote_config_snapshot()
                reconciliation = await self._reconcile_remote_history_on_reconnect(
                    sid, owner_id=owner, _remote_config_snapshot=snapshot
                )
                db = await self._session()
                row = await db.get(ConversationSession, uuid.UUID(sid))
                if row is None or row.deleted_at is not None:
                    raise CLIDevelopmentSessionNotFound()
                if str(row.user_id) != owner:
                    raise CLIDevelopmentSessionForbidden(
                        "session owner mismatch", code="session_owner_mismatch"
                    )
                context = self._context(row)
                run, work = await self._load_latest(db, row.id)
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
                        _remote_config_snapshot=snapshot,
                    )
                    binding = await self._binding_for_row(db, row)
                    _assert_route(binding, provider=MUSE_PROVIDER, model=None, effort=None)
                    await self._resolve_remote_backend_execution(db, row, context, _remote_config_snapshot=snapshot)
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
                    "reconciled": bool(reconciliation.get("reconciled")),
                    "history_reconciled": bool(
                        reconciliation.get("history_reconciled")
                    ),
                    "history_observed": bool(
                        reconciliation.get("history_observed")
                    ),
                    "history_observation": reconciliation.get(
                        "history_observation"
                    )
                    or self._remote_result_fields(run, work).get(
                        "history_observation"
                    ),
                    "reconciliation_diagnostic": str(
                        reconciliation.get("reconciliation_diagnostic")
                        or "history_unconfirmed"
                    ),
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
                    managed_backend=await self._verified_managed_backend(db, row, context),
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

    async def _locked_owned_session_row(self, db: Any, sid: str, owner: str) -> Any:
        row = (
            await db.execute(
                select(ConversationSession)
                .where(ConversationSession.id == uuid.UUID(sid))
                .with_for_update()
            )
        ).scalars().first()
        if row is None or getattr(row, "deleted_at", None) is not None:
            raise CLIDevelopmentSessionNotFound()
        if str(row.user_id) != owner:
            raise CLIDevelopmentSessionForbidden(
                "session owner mismatch", code="session_owner_mismatch"
            )
        return row

    @staticmethod
    def _store_cli_context(row: Any, context: Mapping[str, Any]) -> None:
        next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
        next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = dict(context)
        row.context = next_context

    async def begin_remote_reconnect_receipt(
        self, session_id: Any, *, owner_id: str
    ) -> dict[str, Any] | None:
        """Durably record that one owner reconnect attempt was accepted.

        The receipt replaces only the previous bounded receipt.  It does not
        touch the Muse binding, provider nonce, Run/Work rows or safe_to_send.
        Non-remote sessions return ``None``.
        """

        sid = _safe_uuid(session_id)
        if sid is None:
            raise CLIDevelopmentSessionInvalid("invalid session_id", code="invalid_session_id")
        owner = _identifier(owner_id, field_name="owner_id")
        db = await self._session()
        try:
            row = await self._locked_owned_session_row(db, sid, owner)
            context = self._context(row)
            if not self._is_remote_context(context):
                return None
            try:
                previous = max(0, int(context.get(REMOTE_RECONNECT_SEQUENCE_KEY, 0) or 0))
            except (TypeError, ValueError):
                previous = 0
            receipt = {key: None for key in _REMOTE_RECONNECT_RECEIPT_FIELDS}
            receipt.update(
                {
                    "schema_version": REMOTE_RECONNECT_RECEIPT_SCHEMA_VERSION,
                    "attempt_id": str(uuid.uuid4()),
                    "attempt_sequence": previous + 1,
                    "state": "started",
                    "terminal": False,
                    "started_at": _iso(self._clock()),
                }
            )
            context[REMOTE_RECONNECT_SEQUENCE_KEY] = previous + 1
            context[REMOTE_RECONNECT_RECEIPT_KEY] = receipt
            self._store_cli_context(row, context)
            await db.commit()
            return dict(receipt)
        except Exception:
            await self._rollback_async(db)
            raise
        finally:
            await self._close(db)

    @staticmethod
    def _remote_reconnect_terminal_fields(
        state: str, result: Any, error: BaseException | None
    ) -> tuple[str, dict[str, Any]]:
        if state == "completed" and not isinstance(result, Mapping):
            return "failed", {
                "error_type": None,
                "error_code": "invalid_reconnect_result",
            }
        if state != "completed":
            return state, _receipt_error_fields(error)
        reconciled = result.get("reconciled") is True
        history_reconciled = result.get("history_reconciled") is True
        history_observed = result.get("history_observed") is True
        observation_present = isinstance(result.get("history_observation"), Mapping)
        if history_reconciled:
            history_outcome = "reconciled"
        elif history_observed or observation_present:
            history_outcome = "observed"
        else:
            history_outcome = "not_reconciled"
        return "completed", {
            "history_outcome": history_outcome,
            "reconciled": reconciled,
            "history_reconciled": history_reconciled,
            "history_observed": history_observed,
            "history_observation_present": observation_present,
            "reconciliation_diagnostic": _receipt_token(
                result.get("reconciliation_diagnostic"), _REMOTE_RECONNECT_DIAGNOSTICS
            ),
            "session_status": _receipt_token(
                result.get("status"), CLI_DEVELOPMENT_STATUSES
            ),
            "remote_binding_state": _receipt_token(result.get("remote_binding_state")),
            "run_id": _safe_uuid(result.get("run_id")),
            "work_item_id": _safe_uuid(result.get("work_item_id")),
            "error_type": None,
            "error_code": None,
        }

    async def complete_remote_reconnect_receipt(
        self,
        session_id: Any,
        *,
        owner_id: str,
        attempt_id: Any,
        outcome: str,
        result: Any = None,
        error: BaseException | None = None,
    ) -> bool:
        """Settle the started receipt for ``attempt_id`` exactly once.

        A completion for an attempt that is no longer the latest receipt, or
        that is already terminal, is ignored so a stale/slow attempt never
        overwrites a newer attempt.  Returns whether the receipt was written.
        """

        sid = _safe_uuid(session_id)
        attempt = _safe_uuid(attempt_id)
        if sid is None or attempt is None:
            return False
        owner = _identifier(owner_id, field_name="owner_id")
        requested = outcome if outcome in REMOTE_RECONNECT_TERMINAL_STATES else "failed"
        state, fields = self._remote_reconnect_terminal_fields(requested, result, error)
        db = await self._session()
        try:
            try:
                row = await self._locked_owned_session_row(db, sid, owner)
            except (CLIDevelopmentSessionNotFound, CLIDevelopmentSessionForbidden):
                return False
            context = self._context(row)
            current = context.get(REMOTE_RECONNECT_RECEIPT_KEY)
            if (
                not isinstance(current, Mapping)
                or current.get("attempt_id") != attempt
                or current.get("state") != "started"
            ):
                return False
            receipt = {key: current.get(key) for key in _REMOTE_RECONNECT_RECEIPT_FIELDS}
            receipt.update(fields)
            receipt.update(
                {
                    "state": state,
                    "terminal": True,
                    "finished_at": _iso(self._clock()),
                }
            )
            context[REMOTE_RECONNECT_RECEIPT_KEY] = receipt
            self._store_cli_context(row, context)
            await db.commit()
            return True
        except Exception:
            await self._rollback_async(db)
            raise
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

    async def record_remote_reconciliation_result(
        self,
        *,
        session_id: str,
        run_id: str,
        work_item_id: str,
        owner_id: str,
        recovery_nonce: str,
        assistant_text: str,
        assistant_message_id: str,
        assistant_text_truncated: bool = False,
        assistant_text_original_length: int | None = None,
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically settle one history-confirmed uncertain Muse turn."""

        return await self._record_execution_result(
            session_id=session_id,
            run_id=run_id,
            work_item_id=work_item_id,
            classification="succeeded",
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
            assistant_text_truncated=assistant_text_truncated,
            assistant_text_original_length=assistant_text_original_length,
            _recovery_nonce=recovery_nonce,
            _recovery_owner_id=owner_id,
            _remote_delivery=delivery,
        )

    async def record_execution_result(
        self,
        *,
        session_id: str,
        run_id: str,
        work_item_id: str,
        classification: str,
        output: str | None = None,
        assistant_text: str | None = None,
        usage: Mapping[str, Any] | None = None,
        assistant_message_id: str | None = None,
        assistant_text_truncated: bool = False,
        assistant_text_original_length: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        native_state: str | None = None,
        native_scope_key: str | None = None,
        delegate_worker_usage: Mapping[str, Any] | None = None,
        delegate_worker_result: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
        attempt: int | None = None,
        cli_failure_diagnostic: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a normal provider result under the ordinary late-result fence."""

        return await self._record_execution_result(
            session_id=session_id,
            run_id=run_id,
            work_item_id=work_item_id,
            classification=classification,
            output=output,
            assistant_text=assistant_text,
            usage=usage,
            assistant_message_id=assistant_message_id,
            assistant_text_truncated=assistant_text_truncated,
            assistant_text_original_length=assistant_text_original_length,
            error_code=error_code,
            error_message=error_message,
            native_state=native_state,
            native_scope_key=native_scope_key,
            delegate_worker_usage=delegate_worker_usage,
            delegate_worker_result=delegate_worker_result,
            lease_owner=lease_owner,
            lease_token=lease_token,
            attempt=attempt,
            cli_failure_diagnostic=cli_failure_diagnostic,
        )

    async def _record_execution_result(
        self,
        *,
        session_id: str,
        run_id: str,
        work_item_id: str,
        classification: str,
        output: str | None = None,
        assistant_text: str | None = None,
        usage: Mapping[str, Any] | None = None,
        assistant_message_id: str | None = None,
        assistant_text_truncated: bool = False,
        assistant_text_original_length: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        native_state: str | None = None,
        native_scope_key: str | None = None,
        delegate_worker_usage: Mapping[str, Any] | None = None,
        delegate_worker_result: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
        attempt: int | None = None,
        cli_failure_diagnostic: Mapping[str, Any] | None = None,
        _recovery_nonce: str | None = None,
        _recovery_owner_id: str | None = None,
        _remote_delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist provider evidence after the backend worker has drained.

        The adapter calls this only after ``execute_prompt`` returned (or its
        cancellation cleanup completed).  A coordinator may later settle the
        Work/Run fence; the ordinary path never upgrades an already-settled
        Run.  The private recovery arguments are supplied only by the separate
        reconnect reconciliation entry point above.
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
            safe_cli_failure = (
                sanitize_cli_failure_diagnostic(cli_failure_diagnostic)
                if remote
                else None
            )
            safe_delegate_worker_result = _safe_delegate_worker_result(
                delegate_worker_result
            )
            safe_delegate_worker_usage = _safe_delegate_worker_usage_receipt(
                delegate_worker_usage
            )
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
            recovering_remote = _recovery_nonce is not None
            if recovering_remote:
                remote_binding_context = (
                    row.context.get(MUSE_REMOTE_CONTEXT_KEY)
                    if isinstance(row.context, Mapping)
                    else None
                )
                remote_binding = (
                    remote_binding_context.get("binding")
                    if isinstance(remote_binding_context, Mapping)
                    and isinstance(remote_binding_context.get("binding"), Mapping)
                    else remote_binding_context
                )
                run_result = (
                    run.result if isinstance(getattr(run, "result", None), Mapping) else {}
                )
                work_result = (
                    work.result_summary_json
                    if isinstance(getattr(work, "result_summary_json", None), Mapping)
                    else {}
                )
                # A second reconnect can arrive after the first transaction
                # committed but before its HTTP response reached the caller.
                # The durable receipt plus deterministic message identity is
                # sufficient evidence to return idempotently.
                if (
                    remote
                    and str(run.status or "") == "succeeded"
                    and str(work.state or "") == "succeeded"
                    and run_result.get("remote_history_reconciled") is True
                    and work_result.get("remote_history_reconciled") is True
                    and isinstance(remote_binding, Mapping)
                    and not remote_binding.get("current_turn_nonce")
                    and not remote_binding.get("current_run_id")
                    and str(remote_binding.get("state") or "") == "bound"
                    and remote_binding.get("safe_to_send") is True
                ):
                    existing = (
                        await db.execute(
                            select(ConversationMessage).where(
                                ConversationMessage.session_id == sid,
                                ConversationMessage.client_message_id
                                == f"muse-assistant:{rid}",
                            )
                        )
                    ).scalars().first()
                    expected_provider_id = str(assistant_message_id or "").strip()
                    if (
                        existing is not None
                        and str(existing.content or "") == str(assistant_text or "")
                        and str(
                            (existing.message_metadata or {}).get(
                                "provider_message_id"
                            )
                            or ""
                        )
                        == expected_provider_id
                    ):
                        await db.rollback()
                        return {
                            "status": "completed",
                            "run_id": str(rid),
                            "work_item_id": str(wid),
                            "assistant_message_id": run_result.get(
                                "assistant_message_id"
                            ),
                            "provider_assistant_message_id": expected_provider_id,
                            "history_reconciled": True,
                            "idempotent": True,
                        }
                latest_run, latest_work = await self._load_latest(db, sid)
                recovery_error_code = str(run.error or "")
                recovery_run_event = (
                    await db.execute(
                        select(AgentRunEvent)
                        .where(
                            AgentRunEvent.run_id == rid,
                            AgentRunEvent.event_type == "cli.disconnected",
                            AgentRunEvent.status == "disconnected",
                        )
                        .order_by(desc(AgentRunEvent.sequence))
                        .limit(1)
                    )
                ).scalars().first()
                recovery_work_event = (
                    await db.execute(
                        select(AgentWorkEvent)
                        .where(
                            AgentWorkEvent.work_item_id == wid,
                            AgentWorkEvent.event_type == "work.uncertain",
                            AgentWorkEvent.to_state == "uncertain",
                            AgentWorkEvent.agent_run_id == run.id,
                            AgentWorkEvent.safe_error_code.in_(
                                (
                                    str(MuseErrorCode.SEND_RESPONSE_UNCORRELATED),
                                    _MUSE_CLI_PROCESS_FAILED,
                                )
                            ),
                        )
                        .order_by(desc(AgentWorkEvent.sequence))
                        .limit(1)
                    )
                ).scalars().first()
                run_event_payload = (
                    recovery_run_event.payload
                    if isinstance(
                        getattr(recovery_run_event, "payload", None), Mapping
                    )
                    else {}
                )
                recovery_work_metadata = (
                    work.metadata_json
                    if isinstance(getattr(work, "metadata_json", None), Mapping)
                    else {}
                )
                recovery_run_metadata = (
                    run.run_metadata
                    if isinstance(getattr(run, "run_metadata", None), Mapping)
                    else {}
                )
                recovery_run_cli_metadata = (
                    recovery_run_metadata.get("cli_development")
                    if isinstance(
                        recovery_run_metadata.get("cli_development"), Mapping
                    )
                    else {}
                )
                eligible_recovery = (
                    remote
                    and isinstance(_recovery_nonce, str)
                    and bool(_recovery_nonce)
                    and str(_recovery_owner_id or "")
                    == str(getattr(row, "user_id", "") or "")
                    and str(getattr(run, "user_id", "") or "")
                    == str(_recovery_owner_id or "")
                    and str(run.status or "") == "failed"
                    and str(work.state or "") == "uncertain"
                    and str(work.outcome_classification or "") == "uncertain"
                    and _remote_recovery_error_matches(
                        run_error=run.error,
                        work_error=work.safe_error_code,
                        binding=remote_binding,
                        result_summary=work_result,
                    )
                    and int(work.attempt_count or 0) > 0
                    and int(run.work_item_attempt or 0)
                    == int(work.attempt_count or 0)
                    and getattr(work, "active_agent_run_id", None) in (None, run.id)
                    and str(run.work_item_id or "") == str(work.id)
                    and str(run.session_id or "") == str(row.id)
                    and str(work.conversation_session_id or "") == str(row.id)
                    and str(latest_run.id if latest_run is not None else "")
                    == str(run.id)
                    and str(latest_work.id if latest_work is not None else "")
                    == str(work.id)
                    and str(work.execution_adapter or "") == MUSE_REMOTE_ADAPTER_KEY
                    and str(work.source_type or "") == REMOTE_SESSION_SOURCE_TYPE
                    and str(work.source_id or "") == str(run.id)
                    and str(work.source_revision or "")
                    == str(context.get("agent_revision_id") or "")
                    and str(run.run_type or "") == REMOTE_SESSION_SOURCE_TYPE
                    and str(run.agent_id or "")
                    == str(context.get("agent_id") or row.agent_id or "")
                    and str(run.agent_revision_id or "")
                    == str(context.get("agent_revision_id") or "")
                    and str(work.assigned_agent_id or "")
                    == str(context.get("agent_id") or row.agent_id or "")
                    and str(work.agent_revision_id or "")
                    == str(context.get("agent_revision_id") or "")
                    and recovery_work_metadata.get("owner_id")
                    == str(_recovery_owner_id or "")
                    and recovery_work_metadata.get("remote_target_key")
                    == str(sid)
                    and str(recovery_work_metadata.get("binding_id") or "")
                    == str(context.get("binding_id") or "")
                    and str(
                        recovery_work_metadata.get("binding_snapshot_hash") or ""
                    )
                    == str(context.get("binding_snapshot_hash") or "")
                    and str(recovery_run_cli_metadata.get("binding_id") or "")
                    == str(context.get("binding_id") or "")
                    and str(
                        recovery_run_cli_metadata.get("binding_snapshot_hash") or ""
                    )
                    == str(context.get("binding_snapshot_hash") or "")
                    and str(
                        recovery_run_cli_metadata.get("remote_target_key") or ""
                    )
                    == str(row.id)
                    and isinstance(remote_binding, Mapping)
                    and str(remote_binding.get("owner_id") or "")
                    == str(_recovery_owner_id or "")
                    and str(remote_binding.get("agent_id") or "")
                    == str(context.get("agent_id") or row.agent_id or "")
                    and str(remote_binding.get("revision_id") or "")
                    == str(context.get("agent_revision_id") or "")
                    and str(remote_binding.get("remote_target_key") or "")
                    == str(row.id)
                    and str(remote_binding.get("current_turn_nonce") or "")
                    == _recovery_nonce
                    and str(remote_binding.get("current_run_id") or "")
                    == str(run.id)
                    and recovery_run_event is not None
                    and recovery_work_event is not None
                    and _remote_uncertain_event_matches(
                        recovery_work_event,
                        run_id=run.id,
                        attempt=int(work.attempt_count or 0),
                        error_code=recovery_error_code,
                    )
                    and str(run_event_payload.get("error_code") or "")
                    == recovery_error_code
                )
                if not eligible_recovery or lease_owner is not None or lease_token is not None or attempt is not None:
                    await db.rollback()
                    return {
                        "discarded": True,
                        "status": "interrupted",
                        "run_id": str(rid),
                        "work_item_id": str(wid),
                        "reason": "recovery_fence_lost",
                    }
            # A cancelled/settled Work owns the terminal fence.  Provider
            # threads may finish after an async cancellation, but their late
            # plaintext must never become a new assistant message.
            if not recovering_remote and (
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
            if recovering_remote and normalized != "succeeded":
                await db.rollback()
                return {
                    "discarded": True,
                    "status": "interrupted",
                    "run_id": str(rid),
                    "work_item_id": str(wid),
                    "reason": "recovery_fence_lost",
                }
            safe_worker_usage = _safe_delegate_worker_usage(usage)
            output_content = ""
            output_message_id = None
            provider_message_id = None
            truncated_original_length = None
            remote_artifact_complete: bool | None = None
            if normalized == "succeeded":
                if remote:
                    # Only the provider adapter's confirmed-correlation
                    # fields are accepted.  Never infer a reply from an
                    # arbitrary outcome field or an uncertain history read.
                    if run.trigger_message_id is None:
                        await db.rollback()
                        return {
                            "discarded": True,
                            "status": "interrupted",
                            "run_id": str(rid),
                            "work_item_id": str(wid),
                            "reason": "remote_reply_unconfirmed",
                        }
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
                        provided_original_length
                        if assistant_text_truncated
                        and provided_original_length is not None
                        and provided_original_length >= len(output_content)
                        else None
                    )
                    remote_artifact_complete = not assistant_text_truncated
                    if (
                        not provider_message_id
                        or len(provider_message_id) > 512
                        or any(ord(char) < 0x20 for char in provider_message_id)
                        or not output_content
                        or len(output_content) > MAX_MUSE_OUTPUT_CHARS
                        or (assistant_text_truncated and truncated_original_length is None)
                    ):
                        await db.rollback()
                        return {
                            "discarded": True,
                            "status": "interrupted",
                            "run_id": str(rid),
                            "work_item_id": str(wid),
                            "reason": (
                                "remote_reply_over_limit"
                                if len(output_content) > MAX_MUSE_OUTPUT_CHARS
                                else "remote_reply_unconfirmed"
                            ),
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
                        or str(message.parent_message_id or "")
                        != str(run.trigger_message_id)
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
                                "assistant_artifact_complete": remote_artifact_complete,
                            }
                        )
                        if assistant_text_truncated:
                            message_metadata["assistant_text_truncated"] = True
                            message_metadata[
                                "assistant_text_original_length"
                            ] = truncated_original_length
                    message = ConversationMessage(
                        id=uuid.uuid4(),
                        session_id=sid,
                        parent_message_id=(run.trigger_message_id if remote else None),
                        role="assistant",
                        content=output_content if remote else str(output or ""),
                        client_message_id=assistant_key,
                        sender_type="agent",
                        sender_id=str(run.agent_id),
                        message_metadata=message_metadata,
                        created_at=self._clock(),
                        updated_at=self._clock(),
                        is_active_branch=True,
                        branch_index=0,
                    )
                    db.add(message)
                    row.message_count = int(row.message_count or 0) + 1
                # New messages are still pending when autoflush is disabled.
                # Use this exact message for both persistence and output ranges.
                output_content = str(message.content or "")
                output_message_id = str(message.id)
                if safe_delegate_worker_result is not None:
                    refs = list(safe_delegate_worker_result.get("artifact_refs") or [])
                    refs.append(f"conversation_message:{output_message_id}")
                    safe_delegate_worker_result["artifact_refs"] = list(
                        dict.fromkeys(str(item)[:512] for item in refs if isinstance(item, str))
                    )[:64]
                remote_delivery = (
                    _safe_remote_delivery(_remote_delivery)
                    if recovering_remote and remote
                    else None
                )
                run.result = {
                    "assistant_message_id": output_message_id,
                    **(
                        {
                            "usage": safe_delegate_worker_usage or {}
                        }
                        if isinstance(delegate_worker_usage, Mapping)
                        else {}
                    ),
                    **(
                        {"provider_assistant_message_id": provider_message_id}
                        if remote and provider_message_id
                        else {}
                    ),
                    **(
                        {"assistant_artifact_complete": remote_artifact_complete}
                        if remote
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
                    **(
                        {
                            "turn_status": "confirmed",
                            "delivery": remote_delivery,
                            "remote_history_reconciled": True,
                        }
                        if recovering_remote
                        else {}
                    ),
                }
                run.validation = {
                    "provider_output_persisted": True,
                    **(
                        {"assistant_artifact_complete": remote_artifact_complete}
                        if remote
                        else {}
                    ),
                    **({"remote_history_reconciled": True} if recovering_remote else {}),
                }
                work.result_summary = (
                    "Muse remote turn completed"
                    if remote
                    else "CLI session turn completed"
                )
                if recovering_remote:
                    work.state = "succeeded"
                    work.outcome_classification = "succeeded"
                    work.active_agent_run_id = None
                    work.lease_owner = None
                    work.lease_token = None
                    work.lease_expires_at = None
                    work.safe_error_code = None
                    work.blocker_code = None
                    work.escalation_reason = None
                    work.not_before = None
                    work.next_attempt_at = None
                    work.completed_at = self._clock()
                    work.updated_at = self._clock()
                    work.result_summary_json = {
                        "execution_target": MUSE_EXECUTION_TARGET,
                        "local_filesystem": False,
                        "sandbox": "remote_uncontrolled",
                        "turn_status": "confirmed",
                        "send_attempted": True,
                        "send_attempted_known": True,
                        "acceptance": "acceptance_confirmed",
                        "safe_to_send": True,
                        "delivery": remote_delivery,
                        "assistant_artifact_complete": remote_artifact_complete,
                        "assistant_message_id": output_message_id,
                        "provider_assistant_message_id": provider_message_id,
                        "remote_history_reconciled": True,
                    }
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
            if safe_delegate_worker_usage is not None:
                run_result = dict(run.result) if isinstance(run.result, Mapping) else {}
                run_result["usage"] = safe_delegate_worker_usage
                run.result = run_result
                work_result = dict(work.result_summary_json) if isinstance(work.result_summary_json, Mapping) else {}
                work_result["usage"] = safe_delegate_worker_usage
                work.result_summary_json = work_result
            if safe_delegate_worker_result is not None:
                run_result = dict(run.result) if isinstance(run.result, Mapping) else {}
                run_result["delegate_worker_result"] = safe_delegate_worker_result
                run.result = run_result
                work_result = dict(work.result_summary_json) if isinstance(work.result_summary_json, Mapping) else {}
                work_result["delegate_worker_result"] = safe_delegate_worker_result
                work.result_summary_json = work_result
            if recovering_remote:
                run.status = "succeeded"
                run.error = None
                run.updated_at = self._clock()
                run.ended_at = self._clock()
            elif str(run.status or "") not in {"cancelled", "succeeded", "failed"}:
                if target_status == "completed":
                    run.status = "succeeded"
                elif target_status == "cancelled":
                    run.status = "cancelled"
                elif target_status == "waiting_approval":
                    run.status = "awaiting_approval"
                else:
                    run.status = "failed"
                run.error = str(error_message or error_code or "")[:4000] or None
                if safe_cli_failure is not None:
                    run_result = (
                        dict(run.result)
                        if isinstance(getattr(run, "result", None), Mapping)
                        else {}
                    )
                    run_result["cli_failure_diagnostic"] = safe_cli_failure
                    run.result = run_result
                run.updated_at = self._clock()
                run.ended_at = self._clock() if target_status in CLI_DEVELOPMENT_TERMINAL_STATUSES else None
            context["status"] = target_status
            if native_state:
                context["native_state"] = str(native_state)[:32]
            if native_scope_key:
                context["native_scope_key"] = str(native_scope_key)[:128]
            next_context = copy.deepcopy(row.context) if isinstance(row.context, Mapping) else {}
            if recovering_remote:
                remote_context = next_context.get(MUSE_REMOTE_CONTEXT_KEY)
                remote_binding = (
                    remote_context.get("binding")
                    if isinstance(remote_context, Mapping)
                    and isinstance(remote_context.get("binding"), Mapping)
                    else remote_context
                )
                if (
                    not isinstance(remote_binding, Mapping)
                    or str(remote_binding.get("current_turn_nonce") or "")
                    != _recovery_nonce
                    or str(remote_binding.get("current_run_id") or "")
                    != str(run.id)
                ):
                    await db.rollback()
                    return {
                        "discarded": True,
                        "status": "interrupted",
                        "run_id": str(rid),
                        "work_item_id": str(wid),
                        "reason": "recovery_fence_lost",
                    }
                settled_binding = copy.deepcopy(dict(remote_binding))
                settled_binding.pop("current_turn_nonce", None)
                settled_binding.pop("current_run_id", None)
                try:
                    settled_binding["turn_count"] = max(
                        0, int(settled_binding.get("turn_count", 0) or 0)
                    ) + 1
                except (TypeError, ValueError, OverflowError):
                    settled_binding["turn_count"] = 1
                settled_binding["state"] = "bound"
                settled_binding["safe_to_send"] = True
                if isinstance(remote_context, Mapping) and isinstance(
                    remote_context.get("binding"), Mapping
                ):
                    updated_remote_context = copy.deepcopy(dict(remote_context))
                    updated_remote_context["binding"] = settled_binding
                    next_context[MUSE_REMOTE_CONTEXT_KEY] = updated_remote_context
                else:
                    next_context[MUSE_REMOTE_CONTEXT_KEY] = settled_binding
            next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = context
            row.context = next_context
            if isinstance(context.get("delegate_worker"), Mapping):
                worker_result = {
                    **(safe_delegate_worker_result or {}),
                    "assistant_message_id": output_message_id,
                    "assistant_text_sha256": _sha256(output_content),
                    "assistant_text_length": len(output_content),
                    "classification": normalized,
                }
                if safe_worker_usage is not None:
                    worker_result["usage"] = {**(worker_result.get("usage") or {}), **safe_worker_usage}
                run_result = (
                    dict(run.result)
                    if isinstance(getattr(run, "result", None), Mapping)
                    else {}
                )
                run_result["delegate_worker_result"] = worker_result
                if safe_worker_usage is not None:
                    run_result["usage"] = {**(run_result.get("usage") or {}), **safe_worker_usage}
                run.result = run_result
                work_metadata = (
                    dict(work.metadata_json)
                    if isinstance(getattr(work, "metadata_json", None), Mapping)
                    else {}
                )
                work_metadata["delegate_worker_result"] = copy.deepcopy(
                    worker_result
                )
                work.metadata_json = work_metadata
                work_result = (
                    dict(work.result_summary_json)
                    if isinstance(getattr(work, "result_summary_json", None), Mapping)
                    else {}
                )
                work_result["delegate_worker_result"] = copy.deepcopy(
                    worker_result
                )
                work.result_summary_json = work_result
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
                    if remote and remote_artifact_complete is not None:
                        chunk_payload["assistant_artifact_complete"] = (
                            remote_artifact_complete
                        )
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
            if remote and safe_cli_failure is not None:
                tail_payload["cli_failure_diagnostic"] = safe_cli_failure
            if remote and provider_message_id:
                tail_payload["provider_assistant_message_id"] = provider_message_id
            if remote and remote_artifact_complete is not None:
                tail_payload["assistant_artifact_complete"] = remote_artifact_complete
            if remote and assistant_text_truncated and truncated_original_length is not None:
                tail_payload["assistant_text_truncated"] = True
                tail_payload[
                    "assistant_text_original_length"
                ] = truncated_original_length
            if error_message:
                tail_payload["error_message"] = str(error_message)[:1024]
            db.add(AgentRunEvent(run_id=run.id, sequence=next_sequence, event_type=f"cli.{target_status}", status=target_status, message=str(error_message)[:1024] if error_message else None, payload=tail_payload, created_at=self._clock()))
            if recovering_remote:
                work_tail = (
                    await db.execute(
                        select(AgentWorkEvent)
                        .where(AgentWorkEvent.work_item_id == work.id)
                        .order_by(desc(AgentWorkEvent.sequence))
                        .limit(1)
                    )
                ).scalars().first()
                next_work_sequence = int(
                    getattr(work_tail, "sequence", 0) or 0
                ) + 1
                db.add(
                    AgentWorkEvent(
                        work_item_id=work.id,
                        sequence=next_work_sequence,
                        event_type="work.succeeded",
                        from_state="uncertain",
                        to_state="succeeded",
                        status="succeeded",
                        actor_kind="service",
                        actor_service_key="aoitalk.system",
                        agent_run_id=run.id,
                        payload_json={
                            "classification": "succeeded",
                            "remote_history_reconciled": True,
                            "attempt": int(work.attempt_count or 0),
                        },
                        created_at=self._clock(),
                    )
                )
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
                receipt_dict["assistant_artifact_complete"] = run.result.get(
                    "assistant_artifact_complete"
                )
                if run.result.get("assistant_text_truncated") is True:
                    receipt_dict["assistant_text_truncated"] = True
                    receipt_dict["assistant_text_original_length"] = run.result.get(
                        "assistant_text_original_length"
                    )
                if run.result.get("remote_history_reconciled") is True:
                    receipt_dict["history_reconciled"] = True
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
                    work_adapter = getattr(self, "_muse_work_adapter", None)
                    cancel_remote = getattr(work_adapter, "abort_work", None)
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
        _register_shared_cli_development_session_service(self)
        self.session_store = session_store or self.session_store
        adapter = register_cli_session_work(
            coordinator,
            self,
            backend_factory=backend_factory,
            session_store=self.session_store,
        )

        async def authorized_scope_source(
            identity: Any,
            *,
            session_id: str,
            binding_id: str,
        ) -> Mapping[str, Any] | None:
            # The callback receives only the identity derived from the pinned
            # Agent binding.  The approved scope itself is read from the
            # server-owned store; no request/model field can supply a grant.
            db = await self._session()
            try:
                return await self._read_active_muse_authorized_scope(
                    db,
                    identity,
                    session_id=session_id,
                    binding_id=binding_id,
                )
            finally:
                await self._close(db)

        self._muse_work_adapter = register_muse_remote_session_work(
            coordinator,
            self,
            authorized_scope_source=authorized_scope_source,
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
