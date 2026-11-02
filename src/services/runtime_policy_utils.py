"""Neutral runtime policy helpers shared by Agent work and tool loops."""

from __future__ import annotations

import copy
import contextvars
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

MODEL_PROVIDER_IDS = {
    "openai",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "gemini",
    "ollama",
    "openai_compatible_local",
    "sglang",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}
MODEL_ROUTING_PROVIDERS = MODEL_PROVIDER_IDS | {"claude", "grok"}
AGENT_HARNESS_PROVIDERS = {"codex-cli", "claude-cli"}
MODEL_EXTERNAL_APPROVAL_PROVIDERS = {
    "openai",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "gemini",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}


def _raw_config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read App Config directly from the persisted root."""

    return _raw_config_get(config, key, default)


def config_set(config: Any, key: str, value: Any) -> None:
    if hasattr(config, "set"):
        config.set(key, value)
        return
    if isinstance(config, dict):
        parts = key.split(".")
        target = config
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value


_NON_CIRCUIT_ERROR_CODES = {
    # A command ran inside the already-authorized workspace and returned a
    # non-zero exit. This is task-domain evidence for the model to diagnose,
    # not a failure of the tool runtime itself.
    "command_failed",
    "not_found",
    "ambiguous_target",
    "user_validation",
    "validation",
    "cancelled",
    "docs_access_denied",
    "docs_not_found",
    "docs_ambiguous_target",
    "docs_validation",
    "tool_argument_unknown",
    "tool_argument_conflict",
    "tool_argument_missing",
    "tool_argument_invalid",
    # media_search/media_read argument and scope errors are corrected by the
    # model on the next call; they are not a failing backend.
    "invalid_search_request",
    "invalid_read_request",
    "invalid_locator",
    "conversation_context_required",
    "search_scope_not_permitted",
    "read_scope_not_permitted",
    "query_source_budget_exceeded",
    "evidence_changed",
    "evidence_version_required",
}

# Failure signatures may be copied into circuit-open tool payloads and
# telemetry.  Keep the set closed so a provider/model-controlled value cannot
# smuggle a credential, URL, or document text through an otherwise harmless
# looking ``docs_*``/``provider_*`` token.
_SAFE_FAILURE_CODES = frozenset(
    {
        *_NON_CIRCUIT_ERROR_CODES,
        "command_nonzero_exit",
        # A command tool failure that is neither a verified nonzero exit nor a
        # typed outcome (scope denial, timeout, malformed wrapper) fails closed.
        "command_unclassified_failure",
        "tool_error",
        "circuit_open",
        "docs_access_internal",
        "project_not_found",
        "overview_refresh_failed",
        "overview_refresh_requeue_failed",
        "overview_refresh_invalid_state",
        "provider_http_429",
        "provider_timeout",
        "provider_response_unavailable",
        "client_creation_failed",
        "config_unavailable",
        "empty_response",
        "invalid_json",
        "json_not_object",
        "invalid_route",
        "layout_validation_failed",
        "persistence_failed",
        "unexpected_generation_failure",
        "authenticated_user_required",
        "media_original_unavailable",
        "media_search_internal_error",
        "media_read_internal_error",
    }
)

# Only these codes are an explicit, runtime-owned infrastructure fault that may
# open the per-turn circuit.  Everything else (uncoded failures, an unlisted
# ``error_code`` such as a bad read range, ``tool_error``) is something the
# model can correct by changing its call; a repeat of the *same* call is bounded
# by ``ToolNoProgressGuard`` rather than by this family-wide breaker.
_INTERNAL_CIRCUIT_CODES = frozenset(
    _SAFE_FAILURE_CODES
    - _NON_CIRCUIT_ERROR_CODES
    - {"tool_error", "command_nonzero_exit", "circuit_open"}
)


def is_internal_tool_failure_code(code: Any) -> bool:
    """True only for an explicit infrastructure fault code."""

    return str(code or "").strip().lower() in _INTERNAL_CIRCUIT_CODES


_SAFE_DOCS_DIAGNOSTIC_OPERATIONS = frozenset(
    {
        "docs",
        "docs_read",
        "docs_search",
        "docs_query",
        "docs_create_nodes",
        "docs_update_node",
        "docs_move_node",
        "docs_archive_node",
        "docs_ensure_inbox",
        "docs_attach_workspace_file",
        "docs_place_workspace_file",
        "inbox_search_items",
        "inbox_update_item",
    }
)
_SAFE_DOCS_DIAGNOSTIC_STAGES = frozenset(
    {
        "execution",
        "argument_resolution",
        "database_manager",
        "open_session",
        "authorization",
        "authorization_scope",
        "workspace_resolution",
        "node_resolution",
        "content_read",
        "document_validation",
        "search",
        "render",
        "mutation",
        "copy",
        "approval_receipt",
        "commit",
    }
)
_SAFE_DIAGNOSTIC_EXCEPTION_TYPES = frozenset(
    {
        "attributeerror",
        "dbapierror",
        "fileexistserror",
        "filenotfounderror",
        "integrityerror",
        "keyerror",
        "lookuperror",
        "multipleobjectsreturned",
        "noresultfound",
        "notimplementederror",
        "operationalerror",
        "oserror",
        "permissionerror",
        "runtimeerror",
        "sqlalchemyerror",
        "timeouterror",
        "typeerror",
        "valueerror",
    }
)


_COMMAND_FAMILY_TOOL_NAMES = frozenset(
    {"execute_command", "read_command_output", "write_command_input", "stop_command"}
)


def tool_failure_scope(tool_name: Any, arguments: Any = None) -> str:
    """Digest of one command action (command, cwd, shell, job); empty otherwise.

    Command failures share generic codes, so the breaker keys them per action
    instead of letting one command's fault suppress unrelated commands.
    """

    if str(tool_name or "").rsplit(".", 1)[-1].strip().lower() not in _COMMAND_FAMILY_TOOL_NAMES:
        return ""
    source = arguments if isinstance(arguments, dict) else {}
    identity = {
        key: source.get(key)
        for key in ("command", "working_directory", "shell", "job_id")
        if key in source
    }
    try:
        rendered = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()[:16]


def tool_failure_family(tool_name: Any) -> str:
    """Normalize concrete tool names to a retry/circuit family.

    A model may vary ``docs_search``/``docs_read`` arguments (or move from a
    read to a query helper) while the underlying Docs service is failing for
    the same reason.  Keeping this mapping deterministic prevents those
    cosmetic changes from bypassing the per-turn failure budget.
    """

    name = str(tool_name or "unknown").strip().lower()
    if name.startswith(("docs_", "doc_")) or name in {"search_docs", "read_doc"}:
        return "docs"
    if name.startswith(("project_", "task_", "wbs_", "issue_", "record_")):
        return "project"
    if name.startswith(("workspace_", "filesystem_", "file_")) or name in {
        "read_file",
        "search_files",
        "list_directory",
    }:
        return "workspace"
    # Local semantic search and original reads fail for different reasons
    # (query/scope vs. locator/ACL). Sharing one family let a media_read
    # failure suppress every later media_search in the same turn.
    if name in _COMMAND_FAMILY_TOOL_NAMES:
        # Command runtime faults must not share the catch-all ``unknown``
        # family: that would let one command's scope denial or timeout
        # suppress unrelated file-edit tools in the same turn.
        return "command"
    if name == "media_search":
        return "media_search"
    if name == "media_read":
        return "media_read"
    if name.startswith(("media_", "spotify_")):
        return "media"
    # A provider-controlled function name is not an operator-controlled
    # diagnostic namespace. Never use it verbatim as a breaker key or in a
    # circuit-open message: names can contain arbitrary text (including
    # credentials) and can otherwise poison the retry budget. Unknown tools
    # deliberately share one bounded family; repository-defined prefixes above
    # retain their stable cross-tool families.
    return "unknown"


@dataclass(frozen=True)
class ToolFailureSignature:
    tool_family: str
    error_code: str
    root_cause: str
    # Opaque digest of the failing action (e.g. one command line).  Failures
    # that share a generic code but come from different actions must not open
    # a circuit for each other.
    scope: str = ""

    @property
    def key(self) -> str:
        base = f"{self.tool_family}:{self.error_code}:{self.root_cause}"
        return f"{base}@{self.scope}" if self.scope else base


def _normalize_root_cause(value: Any) -> str:
    text = str(value or "").strip().lower()
    # Search/query parameters are request noise, not a new root cause.
    text = re.sub(r"(?:query|q|limit|depth|offset)\s*[:=]\s*[^\s,;]+", "", text)
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if not text:
        return "unknown"
    # Only stable, repository-defined diagnostic namespaces may be included in
    # a circuit signature.  Generic exception text can contain credentials,
    # URLs, or document content and must never be echoed in model-facing
    # circuit-open payloads or warning logs.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", text):
        return "tool_error"
    if text in _SAFE_FAILURE_CODES:
        return text
    parts = text.split(".")
    if (
        len(parts) == 3
        and parts[0] in _SAFE_DOCS_DIAGNOSTIC_OPERATIONS
        and parts[1] in _SAFE_DOCS_DIAGNOSTIC_STAGES
        and parts[2] in _SAFE_DIAGNOSTIC_EXCEPTION_TYPES
    ):
        return text
    return "tool_error"


def normalize_tool_failure_signature(tool_family: Any, error_code: Any = "", root_cause: Any = "", scope: Any = "") -> ToolFailureSignature:
    family = re.sub(r"[^a-z0-9_.-]+", "_", str(tool_family or "unknown").strip().lower()) or "unknown"
    if family not in {"docs", "project", "workspace", "media", "media_search", "media_read", "command", "unknown"}:
        family = "unknown"
    code = re.sub(r"[^a-z0-9_.-]+", "_", str(error_code or "unknown").strip().lower()) or "unknown"
    if code not in _SAFE_FAILURE_CODES:
        code = "tool_error"
    safe_scope = re.sub(r"[^a-f0-9]+", "", str(scope or "").strip().lower())[:32]
    return ToolFailureSignature(family, code, _normalize_root_cause(root_cause), safe_scope)


def parse_structured_tool_failure(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            # Provider adapters sometimes prefix a high-level Docs tool's
            # structured envelope with a short natural-language ``Error:``
            # label.  Retain the non-retryable classification so the parent
            # circuit breaker cannot be bypassed by cosmetic wording.
            text = str(value or "")
            if "docs" in text.casefold() and "内部処理" in text:
                return {
                    "error_code": "docs_access_internal",
                    "retryable": False,
                    "error": "Docsの内部処理に失敗しました。",
                    "root_cause": "docs_access_internal",
                }
            return None
        payload = parsed if isinstance(parsed, dict) else {}
    else:
        payload = {}
    if payload.get("success", True) is not False:
        return None
    error = str(
        payload.get("error") or payload.get("message") or ""
    )
    diagnostic_code = str(
        payload.get("diagnostic_code") or ""
    ).strip()
    retryable_value = payload.get("retryable", False)
    retryable = retryable_value if isinstance(retryable_value, bool) else False
    return {
        "success": False,
        "error_code": str(payload.get("error_code") or payload.get("code") or "tool_error"),
        "retryable": retryable,
        "error": error,
        "root_cause": diagnostic_code or error,
    }


@dataclass(frozen=True)
class ToolFailureDecision:
    allowed: bool
    retryable: bool
    signature: ToolFailureSignature | None = None
    count: int = 0
    failed_tool_count: int = 0
    reason: str = ""
    circuit_opened: bool = False


class ToolFailureCircuitBreaker:
    """Bound repeated non-retryable tool failures per delegation turn."""

    def __init__(self, *, max_same_failure: int = 2, failed_tool_budget: int = 8) -> None:
        self.max_same_failure = max(1, int(max_same_failure))
        self.failed_tool_budget = max(1, int(failed_tool_budget))
        self._counts: dict[str, int] = {}
        self._opened: set[str] = set()
        self._failed_tool_count = 0
        self._suppressed: dict[str, int] = {}

    @property
    def failed_tool_count(self) -> int:
        return self._failed_tool_count

    def check(self, tool_family: str, failure: Any = None, *, error_code: str = "", retryable: bool | None = None, root_cause: str = "", scope: str = "") -> ToolFailureDecision:
        parsed = parse_structured_tool_failure(failure)
        if parsed:
            error_code = parsed["error_code"]
            retryable = parsed["retryable"]
            root_cause = parsed.get("root_cause") or parsed["error"]
        if retryable is None:
            retryable = False
        signature = normalize_tool_failure_signature(tool_family, error_code, root_cause, scope)
        code = signature.error_code
        if code in _NON_CIRCUIT_ERROR_CODES or code not in _INTERNAL_CIRCUIT_CODES:
            return ToolFailureDecision(True, bool(retryable), signature=signature, reason="user-resolvable failure")
        if bool(retryable):
            return ToolFailureDecision(True, True, signature=signature, reason="retryable failure")
        if signature.key in self._opened:
            self._suppressed[signature.key] = self._suppressed.get(signature.key, 0) + 1
            return ToolFailureDecision(False, False, signature=signature, count=self._counts.get(signature.key, 0), failed_tool_count=self._failed_tool_count, reason="circuit open", circuit_opened=True)
        self._failed_tool_count += 1
        count = self._counts.get(signature.key, 0) + 1
        self._counts[signature.key] = count
        if count >= self.max_same_failure or self._failed_tool_count >= self.failed_tool_budget:
            self._opened.add(signature.key)
            return ToolFailureDecision(False, False, signature=signature, count=count, failed_tool_count=self._failed_tool_count, reason="circuit opened", circuit_opened=True)
        return ToolFailureDecision(True, False, signature=signature, count=count, failed_tool_count=self._failed_tool_count, reason="failure budget available")

    def allow(self, tool_family: str, failure: Any = None, **kwargs: Any) -> bool:
        return self.check(tool_family, failure, **kwargs).allowed

    def is_open(self, tool_family: str, scope: str = "") -> bool:
        """Return whether a normalized failure in ``tool_family`` is open.

        With ``scope`` only unscoped keys and keys of that exact action count,
        so another action's failure never suppresses this one.
        """

        family = str(tool_family or "unknown").strip().lower()
        safe_scope = re.sub(r"[^a-f0-9]+", "", str(scope or "").strip().lower())[:32]
        for key in self._opened:
            if not key.startswith(f"{family}:"):
                continue
            if not safe_scope or "@" not in key or key.endswith(f"@{safe_scope}"):
                return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {"failed_tool_count": self._failed_tool_count, "counts": dict(self._counts), "opened": sorted(self._opened), "suppressed": dict(self._suppressed)}


@dataclass
class AgentContinuationState:
    original_goal: str = ""
    selected_project_id: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    resolved_node_ids: tuple[str, ...] = ()
    # Bounded read context retained for a continuation.  This is deliberately
    # opaque text (not a full corpus dump) so a cancelled Docs write can carry
    # the source facts into the resumed child without re-searching the steer
    # wording or persisting huge payloads in AgentRun metadata.
    resolved_context: tuple[str, ...] = ()
    successful_tool_identities: tuple[str, ...] = ()
    pending_mutation_target_ids: tuple[str, ...] = ()
    pending_destination_parent_id: str | None = None
    mutation_state: str = "not_started"
    cancelled_tool_events: list[dict[str, Any]] = field(default_factory=list)
    explicit_cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_goal": self.original_goal,
            "selected_project_id": self.selected_project_id,
            "scope": copy.deepcopy(self.scope),
            "resolved_node_ids": list(self.resolved_node_ids),
            "resolved_context": list(self.resolved_context),
            "successful_tool_identities": list(self.successful_tool_identities),
            "pending_mutation_target_ids": list(self.pending_mutation_target_ids),
            "pending_destination_parent_id": self.pending_destination_parent_id,
            "mutation_state": self.mutation_state,
            "cancelled_tool_events": copy.deepcopy(self.cancelled_tool_events),
            "explicit_cancelled": self.explicit_cancelled,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AgentContinuationState":
        raw = value if isinstance(value, dict) else {}
        return cls(
            original_goal=str(raw.get("original_goal") or ""),
            selected_project_id=str(raw.get("selected_project_id") or "") or None,
            scope=copy.deepcopy(raw.get("scope") or {}) if isinstance(raw.get("scope"), dict) else {},
            resolved_node_ids=tuple(str(item) for item in (raw.get("resolved_node_ids") or []) if str(item)),
            resolved_context=tuple(
                str(item)[:2000]
                for item in (raw.get("resolved_context") or [])
                if str(item).strip()
            )[:8],
            successful_tool_identities=tuple(str(item) for item in (raw.get("successful_tool_identities") or []) if str(item)),
            pending_mutation_target_ids=tuple(str(item) for item in (raw.get("pending_mutation_target_ids") or []) if str(item)),
            pending_destination_parent_id=str(raw.get("pending_destination_parent_id") or "") or None,
            mutation_state=str(raw.get("mutation_state") or "not_started"),
            cancelled_tool_events=copy.deepcopy(raw.get("cancelled_tool_events") or []) if isinstance(raw.get("cancelled_tool_events"), list) else [],
            explicit_cancelled=bool(raw.get("explicit_cancelled", False)),
        )

    def apply_delta(self, message: Any) -> "AgentContinuationState":
        text = str(message or "").strip()
        if text and any(token in text.lower() for token in ("やめて", "中止", "キャンセル", "cancel", "stop")):
            self.explicit_cancelled = True
        return self

    @property
    def can_resume(self) -> bool:
        return not self.explicit_cancelled and self.mutation_state not in {"completed", "cancelled"}


_CURRENT_CONTINUATION_STATE: contextvars.ContextVar[AgentContinuationState | None] = (
    contextvars.ContextVar("aoitalk_current_continuation_state", default=None)
)


def set_current_continuation_state(
    state: AgentContinuationState | None,
) -> contextvars.Token:
    """Bind the active tool-loop continuation to the current turn.

    The binding intentionally lives for the generation attempt (rather than
    being reset at the end of the delegate call).  ``GenerationInterrupted``
    is caught one frame above the tool loop, so the response retry can include
    the deterministic state even when the delegate coroutine was cancelled.
    ContextVars keep this turn-local and do not leak to another conversation.
    """

    return _CURRENT_CONTINUATION_STATE.set(state)


def reset_current_continuation_state(token: contextvars.Token) -> None:
    """Restore the continuation binding that preceded a generation attempt."""

    _CURRENT_CONTINUATION_STATE.reset(token)


def get_current_continuation_state() -> AgentContinuationState | None:
    return _CURRENT_CONTINUATION_STATE.get()


def continuation_state_for_prompt(
    state: AgentContinuationState | None = None,
) -> str:
    """Render a bounded, provider-neutral continuation snapshot for retry."""

    current = state if state is not None else get_current_continuation_state()
    if current is None:
        return ""
    payload = current.to_dict()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def make_continuation_state(**kwargs: Any) -> AgentContinuationState:
    return AgentContinuationState.from_dict(kwargs)


def cancelled_tool_terminal_event(tool_name: str, *, reason: str = "user_interrupt", state: AgentContinuationState | None = None) -> dict[str, Any]:
    payload = {"event_type": "tool_end", "tool": str(tool_name), "status": "cancelled", "reason": str(reason or "user_interrupt")}
    if state is not None:
        payload["continuation_state"] = state.to_dict()
        state.cancelled_tool_events.append(dict(payload))
    return payload


def apply_continuation_delta(state: AgentContinuationState | dict[str, Any], message: Any) -> AgentContinuationState:
    result = state if isinstance(state, AgentContinuationState) else AgentContinuationState.from_dict(state)
    return result.apply_delta(message)


def managed_tools_for_capabilities(
    capabilities: Any = (),
    *,
    tools_required: bool = True,
    work_mode: str = "read",
    backend: str = "api",
    config: Any = None,
) -> list[Any]:
    """Build neutral managed project/docs tools for a bounded worker.

    This is the capability bridge formerly coupled to Team specialist
    construction. It intentionally contains only generic project/docs ACL
    filtering and is safe for tests and v2 role runtimes.
    """

    if not tools_required:
        return []
    from ..llm.tool_policy import (
        DOCS_MUTATION_TOOL_NAMES,
        DOCS_READ_TOOL_NAMES,
        PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
        PROJECT_MANAGEMENT_READ_TOOL_NAMES,
    )
    requested = {str(item).strip() for item in (capabilities or ()) if str(item).strip()}
    tools: list[Any] = []
    if "project_read" in requested or "project_write" in requested:
        try:
            from ..agents.project_management_agent import ProjectManagementAgent
            from ..tools.core import ensure_tool_definitions
            for item in ProjectManagementAgent(model="").agent.tools:
                is_read = item.name in PROJECT_MANAGEMENT_READ_TOOL_NAMES
                is_mutation = item.name in PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
                if not (
                    (is_read and "project_read" in requested)
                    or (is_mutation and "project_write" in requested and work_mode == "write")
                ):
                    continue
                tools.append(
                    replace(
                        item,
                        side_effect="mutation" if is_mutation else "none",
                        risk="medium" if is_mutation else "low",
                        requires_approval=is_mutation,
                        supports_parallel=not is_mutation,
                    )
                )
        except Exception:
            pass
    if "docs_read" in requested or "docs_write" in requested:
        try:
            from ..tools.core import ensure_tool_definitions
            from ..tools.docs_direct import build_docs_direct_tools

            for item in ensure_tool_definitions(build_docs_direct_tools()):
                is_read = item.name in DOCS_READ_TOOL_NAMES
                is_mutation = item.name in DOCS_MUTATION_TOOL_NAMES
                if not (
                    (is_read and "docs_read" in requested)
                    or (is_mutation and "docs_write" in requested and work_mode == "write")
                ):
                    continue
                update = {
                    "side_effect": "mutation" if is_mutation else "none",
                    "risk": "medium" if is_mutation else "low",
                    "requires_approval": is_mutation,
                    "supports_parallel": not is_mutation,
                }
                tools.append(replace(item, **update) if hasattr(item, "side_effect") else item)
        except Exception:
            pass
    unique: dict[str, Any] = {}
    for item in tools:
        name = str(getattr(item, "name", "") or "")
        if name:
            unique.setdefault(name, item)
    return list(unique.values())
