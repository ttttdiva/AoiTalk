"""Agent Team configuration and runtime helpers.

The persisted Agent Team contract is schema v3 (Team -> Subagent, with
Team-scoped Execution Profiles).  Legacy conversion is isolated in the
dedicated migration module and is never consulted by normal runtime helpers.
"""

from __future__ import annotations

import copy
import contextvars
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


AGENT_TEAM_PROVIDERS = {
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
MODEL_ROUTING_PROVIDERS = AGENT_TEAM_PROVIDERS | {"claude", "grok"}
AGENT_HARNESS_PROVIDERS = {"codex-cli", "claude-cli"}
AGENT_TEAM_EXTERNAL_APPROVAL_PROVIDERS = {
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
    """Read App Config without synthesizing a legacy Team topology.

    Free Team owns only routing-profile/LLM Profile overlays.  It does not
    replace the canonical ``agent_team`` Team/Subagent graph, so reads are
    always served from the App Config root.
    """

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


def _catalog_main_effort(config: Any, provider: str, model: str) -> str:
    """Return the configured Main effort only when the active model accepts it.

    Agent Team ``same``/``lower`` policies are relative to the *resolved Chat
    Main* route.  Main effort used to be exposed here for OpenAI only, which
    meant a DeepSeek/DeepInfra/CLI Main silently lost its configured catalog
    value before the policy resolver ran.  Keep this boundary provider
    agnostic: read the provider's existing config key, then validate the raw
    value against the shared model catalog.  No provider/model-specific effort
    list is maintained here.
    """

    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    if not provider_id or not model_id:
        return ""
    effort_keys = {
        "openai": "openai.reasoning_effort",
        "deepseek": "deepseek.reasoning_effort",
        "deepinfra": "deepinfra.reasoning_effort",
        "kimi": "kimi.reasoning_effort",
        "codex-cli": "codex_cli.reasoning_effort",
        "claude-cli": "claude_cli.reasoning_effort",
        "openai_compatible_local": "openai_compatible_local.llama_cpp.reasoning_effort",
    }
    key = effort_keys.get(provider_id)
    if not key:
        return ""
    effort = str(_raw_config_get(config, key, "") or "").strip()
    # The non-Team DeepSeek request path treats an unset/empty setting as its
    # provider default (high).  Expose that same effective value to Main route
    # resolution so inherit same/lower policies are based on what the request
    # will actually send, rather than silently losing the effort at this
    # catalog boundary.
    if not effort and provider_id == "deepseek":
        effort = "high"
    # gpt-5.6-luna is the configured OpenAI Main fallback.  Preserve the
    # existing fallback, but still pass it through the catalog check below.
    if not effort and provider_id == "openai":
        model_leaf = model_id.lower().rsplit("/", 1)[-1]
        if model_leaf.startswith("gpt-5.6-luna"):
            effort = "max"
    if not effort and provider_id == "openai_compatible_local":
        try:
            from .llm_model_catalog import reasoning_effort_default_for_model

            effort = str(
                reasoning_effort_default_for_model(provider_id, model_id) or ""
            ).strip()
        except Exception:
            effort = ""
    if not effort:
        return ""
    try:
        from .llm_model_catalog import reasoning_effort_options_for_model

        options = [
            str(item).strip()
            for item in reasoning_effort_options_for_model(provider_id, model_id)
            if str(item).strip()
        ]
    except Exception:
        options = []
    if effort in options:
        return effort
    # OpenAI deployments can expose private/tenant model IDs that are absent
    # from the static catalog.  Preserve the pre-existing configured Main
    # value for that unknown model; explicit Agent Team routes still fail
    # closed in ``_apply_explicit_route_effort`` once their target model is
    # resolved.  Providers with a formal catalog return an empty value when a
    # configured effort is not supported.
    if not options and provider_id == "openai":
        return effort
    return ""


def _main_route(config: Any) -> dict[str, Any]:
    """Resolve the configured main provider/model for Profile inheritance."""

    provider = str(_raw_config_get(config, "llm_provider", "openai") or "openai").strip().lower()
    model = str(_raw_config_get(config, "llm_model", "") or "").strip()
    if not model:
        provider_model_keys = {
            "openai": ("openai.model",),
            "gemini": ("gemini.model",),
            "openrouter": ("openrouter.model",),
            "deepseek": ("deepseek.model",),
            "deepinfra": ("deepinfra.model",),
            "kimi": ("kimi.model",),
            "ollama": ("ollama.model",),
            "sglang": ("sglang.model",),
            "openai_compatible_local": ("openai_compatible_local.model",),
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "grok-cli": ("grok_cli.model",),
        }
        for key in provider_model_keys.get(provider, ()):
            model = str(_raw_config_get(config, key, "") or "").strip()
            if model:
                break
    route: dict[str, Any] = {"provider": provider, "model": model}
    effort = _catalog_main_effort(config, provider, model)
    if effort:
        route["effort"] = effort
        route["reasoning_effort"] = effort
    return route


def _agent_team_section(config: Any) -> dict[str, Any]:
    raw = _raw_config_get(config, "agent_team", {}) if config is not None else {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Context resolver compatibility entrypoint


def _legacy_agent_team_scope_active(
    config: Any,
    *,
    user: Any = None,
    project: Any = None,
    session: Any = None,
    generation_profile: str | None = None,
    app_target_id: str | None = None,
    development_status: str | None = None,
    story_mode: str | None = None,
) -> dict[str, Any]:
    from .agent_team_v3 import agent_team_scope_active

    return agent_team_scope_active(
        config,
        user=user,
        project=project,
        session=session,
        generation_profile=generation_profile,
        app_target_id=app_target_id,
        development_status=development_status,
        story_mode=story_mode,
    )


# ---------------------------------------------------------------------------
# Runtime resilience: normalized failures, circuit breaker, and interruption
# continuation state.  These are in-memory/request-scoped by design; no DB
# table or migration is required.

_NON_CIRCUIT_ERROR_CODES = {"not_found", "ambiguous_target", "user_validation", "validation", "cancelled"}


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
    if name.startswith(("media_", "spotify_")):
        return "media"
    return name or "unknown"


@dataclass(frozen=True)
class ToolFailureSignature:
    tool_family: str
    error_code: str
    root_cause: str

    @property
    def key(self) -> str:
        return f"{self.tool_family}:{self.error_code}:{self.root_cause}"


def _normalize_root_cause(value: Any) -> str:
    text = str(value or "").strip().lower()
    # Search/query parameters are request noise, not a new root cause.
    text = re.sub(r"(?:query|q|limit|depth|offset)\s*[:=]\s*[^\s,;]+", "", text)
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:240] or "unknown"


def normalize_tool_failure_signature(tool_family: Any, error_code: Any = "", root_cause: Any = "") -> ToolFailureSignature:
    family = re.sub(r"[^a-z0-9_.-]+", "_", str(tool_family or "unknown").strip().lower()) or "unknown"
    code = re.sub(r"[^a-z0-9_.-]+", "_", str(error_code or "unknown").strip().lower()) or "unknown"
    return ToolFailureSignature(family, code, _normalize_root_cause(root_cause))


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
                }
            return None
        payload = parsed if isinstance(parsed, dict) else {}
    else:
        payload = {}
    if payload.get("success", True) is not False:
        return None
    return {
        "error_code": str(payload.get("error_code") or payload.get("code") or "tool_error"),
        "retryable": bool(payload.get("retryable", False)),
        "error": str(payload.get("error") or payload.get("message") or ""),
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

    def check(self, tool_family: str, failure: Any = None, *, error_code: str = "", retryable: bool | None = None, root_cause: str = "") -> ToolFailureDecision:
        parsed = parse_structured_tool_failure(failure)
        if parsed:
            error_code = parsed["error_code"]
            retryable = parsed["retryable"]
            root_cause = parsed["error"]
        if retryable is None:
            retryable = False
        signature = normalize_tool_failure_signature(tool_family, error_code, root_cause)
        code = signature.error_code
        if code in _NON_CIRCUIT_ERROR_CODES:
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

    def is_open(self, tool_family: str) -> bool:
        """Return whether any normalized failure in ``tool_family`` is open."""

        family = str(tool_family or "unknown").strip().lower()
        return any(key.startswith(f"{family}:") for key in self._opened)

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
    """Bind the active Agent Team continuation to the current turn.

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

# ---------------------------------------------------------------------------
# Canonical schema-v3 runtime surface
# ---------------------------------------------------------------------------

from .agent_team_v3 import (  # noqa: E402,F401
    AGENT_TEAM_CAPABILITY_CATALOG,
    AGENT_TEAM_CONTEXT_TAGS,
    AGENT_TEAM_SCHEMA_VERSION,
    AGENT_TEAM_SUBAGENT_CATALOG,
    agent_team_scope_active,
    agent_team_teams,
    agent_team_subagents,
    agent_team_subagent,
    agent_team_llm_profiles,
    resolve_subagent_route,
    resolve_agent_team_scope,
    filter_subagent_capabilities,
    agent_team_v3_enabled as _agent_team_schema_v3_enabled,
    agent_team_v3_delegation_enabled as _agent_team_v3_delegation_enabled,
    agent_team_v3_context_tags,
    agent_team_v3_profiles,
    agent_team_v3_subagents,
    agent_team_v3_teams,
    agent_team_v3_visible_subagents,
    filter_agent_team_capabilities,
    subagent_requires_external_approval,
    apply_subagent_mode,
    normalize_agent_team_v3,
    resolve_agent_execution_backend,
    resolve_agent_team_v3_route,
)

def agent_team_schema_v3_enabled(config: Any) -> bool:
    return bool(_agent_team_schema_v3_enabled(config))


agent_team_v3_enabled = agent_team_schema_v3_enabled
agent_team_v3_delegation_enabled = _agent_team_v3_delegation_enabled


def agent_team_enabled(config: Any) -> bool:
    """Canonical Agent Team v3 topology gate.

    The old service exposed a second, settings-driven switch alongside the
    Team graph.  Runtime callers now use the schema-v3 gate directly so an
    invalid/pre-v3 section cannot accidentally enable the v3 routing path.
    """

    return bool(agent_team_v3_enabled(config))


def agent_team_delegation_enabled(config: Any) -> bool:
    """Return the persisted schema-v3 delegation switch."""

    return bool(agent_team_v3_delegation_enabled(config))


def agent_team_orchestration_mode(config: Any) -> str:
    """Return the canonical orchestration mode for terminal/API callers."""

    if not agent_team_v3_enabled(config):
        return "standard"
    section = _agent_team_section(config)
    return "director" if section.get("orchestration_mode") == "director" else "standard"
