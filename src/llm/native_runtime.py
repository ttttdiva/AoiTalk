"""AoiTalk-native agent runtime.

The runtime owns the model/tool loop instead of delegating it to a provider SDK.
Provider clients are transport details; tools, retries, and callback events stay
inside AoiTalk.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from ..tools.core import ToolDefinition, ensure_tool_definition
from ..tools.registry import ToolRegistry
from .context_compression import model_tool_result_payload
from .conversation_context import (
    PromptMessages,
    ProviderState,
    normalize_usage,
    prompt_text,
    stable_cache_key,
    stable_tool_schemas,
)
from .agentic_completion import (
    DETERMINISTIC_TASK_MUTATION_TOOLS,
    _simple_task_has_unexpected_successful_mutation,
    _simple_task_request_is_explicit,
    requested_deterministic_task_mutation_tools,
    simple_task_mutation_completion_state,
    SimpleTaskMutationCompletionState,
    successful_empty_task_search,
)
from .context_budget import resolve_context_budget
from .context_snapshot import (
    component,
    last_role_contains_text,
    message_components,
    reconcile_snapshot,
    snapshot,
    tool_components,
    without_text_from_last_role,
)
from .openrouter_provider_routing import merge_provider_options_into_extra_body
from .turn_stream_events import thinking_text_from_message
from .unified_turn_runtime import (
    RegistryToolRouter,
    UnifiedToolCall,
    observable_tool_name,
)
from .generation_error import (
    GenerationFailure,
    classify_generation_error,
    empty_response_failure,
)
from .generation_cancellation import PlanningInteractionTerminated
from ..services.agent_team_service import ToolFailureCircuitBreaker
from ..services.agent_team_v3 import agent_team_v3_delegation_enabled
from ..services.outbound_privacy_service import (
    EgressDescriptor,
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
)
from .planning_policy import (
    PlanningRunPhase,
    get_current_planning_run_state,
)

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _help_turn_is_isolated() -> bool:
    """Return whether the current low-level native turn is Guide-only."""

    try:
        from ..services.turn_context import get_turn_context

        return bool(getattr(get_turn_context(), "suppress_automatic_context", False))
    except Exception:
        # A missing/legacy turn context is an ordinary native call, not an
        # authorization signal for the restricted Help mode.
        return False


def _simple_task_create_execution_allowed(
    user_input: str | None,
    records: list["ToolExecutionRecord"],
) -> bool:
    """Require a proven empty duplicate search before simple task creation."""

    if not _simple_task_request_is_explicit(str(user_input or "")):
        return True
    if requested_deterministic_task_mutation_tools(user_input) != {"create_task"}:
        return True
    if _simple_task_has_unexpected_successful_mutation(records):
        return False
    return successful_empty_task_search(records) is True


def _simple_task_unexpected_mutation_execution_blocked(
    user_input: str | None,
    tool_name: str,
    records: list["ToolExecutionRecord"],
) -> bool:
    """Keep the simple-create native tool surface to search -> create.

    A model batch can contain several function calls.  Waiting until the
    later ``create_task`` call to reject an unexpected update/delete would
    still commit that first side effect.  Block the call before dispatch and
    stop the rest of the batch; failed/synthetic audit records remain visible.
    """

    if not _simple_task_request_is_explicit(str(user_input or "")):
        return False
    if requested_deterministic_task_mutation_tools(user_input) != {"create_task"}:
        return False
    unexpected = DETERMINISTIC_TASK_MUTATION_TOOLS - {"create_task"}
    if str(tool_name or "").strip().casefold() not in unexpected:
        return False
    return not any(
        record.tool.casefold() == "create_task" and record.successful
        for record in records
    )


# ``planning_runtime`` imports the provider-neutral LLM package during normal
# application startup.  Keep this bridge lazy so importing that service first
# cannot cycle through manager -> Gemini -> native_runtime -> planning_runtime.
def get_approved_action_directive():
    from ..services.planning_runtime import get_approved_action_directive as resolve

    return resolve()


def bind_approved_action_call(*args, **kwargs):
    from ..services.planning_runtime import bind_approved_action_call as bind

    return bind(*args, **kwargs)


@contextmanager
def cloud_advisor_parent_scope(
    *,
    origin: Any | None = None,
    assessment: Any | None = None,
):
    """Bind trusted Cloud Advisor invocation metadata for a parent turn.

    This is a deliberately small bridge between request/controller code and
    the canonical ``consult_cloud_advisor`` tool.  Trigger origin and
    automatic-escalation assessment live in a task-local ContextVar owned by
    ``cloud_advisor_service``; they are never accepted as model tool
    arguments.  Invalid/missing values fail closed to Main-agent + an empty
    semantic assessment.  Agent Team child denial remains enforced by the
    service/tool-policy boundary.

    Callers that already bind the metadata through ``set_turn_context`` may
    omit both arguments.  The context is inherited by asynchronous work in
    the parent turn and restored on exit, so concurrent sessions cannot leak
    origin or assessment state.
    """

    from ..services.cloud_advisor_service import (
        CloudAdvisorEscalationAssessment,
        CloudAdvisorTriggerOrigin,
        cloud_advisor_invocation_scope,
        get_cloud_advisor_invocation_context,
    )
    from ..services.turn_context import get_turn_context

    turn = get_turn_context()
    current = get_cloud_advisor_invocation_context()

    effective_origin = origin
    if effective_origin is None:
        effective_origin = getattr(turn, "cloud_advisor_origin", None)
    if effective_origin is None:
        effective_origin = current.origin
    try:
        if not isinstance(effective_origin, CloudAdvisorTriggerOrigin):
            effective_origin = CloudAdvisorTriggerOrigin(
                str(effective_origin).strip().casefold()
            )
    except (TypeError, ValueError):
        effective_origin = CloudAdvisorTriggerOrigin.MAIN_AGENT

    effective_assessment = assessment
    if effective_assessment is None:
        effective_assessment = getattr(
            turn,
            "cloud_advisor_assessment",
            None,
        )
    if not isinstance(effective_assessment, CloudAdvisorEscalationAssessment):
        # Never coerce arbitrary mappings/booleans into escalation authority.
        effective_assessment = (
            current.assessment
            if isinstance(current.assessment, CloudAdvisorEscalationAssessment)
            else CloudAdvisorEscalationAssessment()
        )

    with cloud_advisor_invocation_scope(
        origin=effective_origin,
        assessment=effective_assessment,
    ):
        yield


async def get_approved_action_receipt(*args, **kwargs):
    from ..services.planning_runtime import get_approved_action_receipt as read

    return await read(*args, **kwargs)


async def accept_approved_action_receipt(*args, **kwargs):
    from ..services.planning_runtime import accept_approved_action_receipt as accept

    return await accept(*args, **kwargs)


async def persist_and_accept_approved_action_result(*args, **kwargs):
    from ..services.planning_runtime import (
        persist_and_accept_approved_action_result as persist,
    )

    return await persist(*args, **kwargs)


async def fail_approved_action(*args, **kwargs):
    from ..services.planning_runtime import fail_approved_action as fail

    return await fail(*args, **kwargs)


def _normalized_usage(
    usage: Any,
    *,
    provider: str = "",
    resolved_model: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_usage(
        usage, provider=provider, resolved_model=resolved_model
    )
    if not normalized:
        return None
    if normalized.get("input_tokens") is None and normalized.get("output_tokens") is None:
        return None
    # Keep the long-standing compact shape for SDK payloads that only expose
    # prompt/completion tokens.  Rich fields are included when the provider
    # actually reported them, so diagnostics remain lossless without breaking
    # downstream callers that compare the legacy shape.
    result = {
        key: normalized.get(key)
        for key in ("input_tokens", "output_tokens", "cached_tokens")
        if normalized.get(key) is not None
    }
    for key in (
        "reasoning_tokens",
        "cache_read_tokens",
        "prompt_eval_tokens",
        "prompt_eval_ms",
        "cache_hit_rate",
        "cache_evictions",
        "cache_provider",
        "cache_mode",
        "cache_key",
        "cache_supported",
        "cache_active",
        "metrics_source",
    ):
        raw_value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if raw_value is not None and normalized.get(key) is not None:
            result[key] = normalized[key]
    # normalize_usage が入れ子（prompt_tokens_details 等）からしか拾えない、あるいは
    # 実際に報告されたときだけ追加する課金情報は正規化結果の有無だけで判定する。
    for key in (
        "cache_write_tokens",
        "provider_reported_cost",
        "provider_reported_cost_details",
        "resolved_model",
        "tool_invocations",
    ):
        value = normalized.get(key)
        if value is not None:
            result[key] = value
    return result


@dataclass(frozen=True)
class Reasoning:
    effort: Optional[str] = None


@dataclass(frozen=True)
class NativeModelSettings:
    tool_choice: Optional[str] = None
    reasoning: Optional[Reasoning] = None


@dataclass
class AgentDefinition:
    name: str
    instructions: str
    model: str
    tools: list[ToolDefinition] = field(default_factory=list)
    model_settings: NativeModelSettings = field(default_factory=NativeModelSettings)

    def __post_init__(self) -> None:
        self.tools = [ensure_tool_definition(tool) for tool in self.tools]


@dataclass(frozen=True)
class ToolExecutionRecord:
    tool: str
    arguments: dict[str, Any]
    result: str

    @property
    def successful(self) -> bool:
        lowered = self.result.strip().lower()
        if lowered.startswith(("{", "[")):
            try:
                payload = json.loads(self.result)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                marker = payload.get("success")
                if marker is not None and str(marker).strip().casefold() not in {
                    "1",
                    "true",
                    "yes",
                    "ok",
                    "success",
                    "succeeded",
                }:
                    return False
                if str(payload.get("error") or "").strip():
                    return False
        return not (
            lowered.startswith("tool not found:")
            or lowered.startswith("error:")
            or lowered.startswith("tool execution error:")
        )


@dataclass(frozen=True)
class NativeRunResult:
    final_output: str
    messages: list[dict[str, Any]]
    tool_calls: list[ToolExecutionRecord] = field(default_factory=list)
    usage_records: list[dict[str, Any]] = field(default_factory=list)
    context_snapshots: list[dict[str, Any]] = field(default_factory=list)
    # ツールループ上限に達して最終応答を作れないまま打ち切ったか。
    # True の run は成功扱いにせず failed として記録する。
    tool_rounds_exhausted: bool = False
    # A failed native turn may carry the same structured failure classification
    # used by ResponseHandler.  Normal successful results keep this ``None``.
    generation_failure: GenerationFailure | None = None


def _safe_observed_value(value: Any, *, limit: int = 240) -> str:
    """Render provider fields for diagnostics without assuming a schema."""

    if value is None:
        return ""
    try:
        if isinstance(value, (dict, list, tuple)):
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        else:
            rendered = str(value)
    except Exception:
        rendered = repr(value)
    rendered = rendered.replace("\n", " ").strip()
    return rendered[:limit]


def _empty_response_failure(
    *,
    transport: str,
    response: Any,
    finish_reason: Any = None,
    choice: Any = None,
    output_items: Iterable[Any] | None = None,
) -> GenerationFailure:
    """Build an ``EMPTY_RESPONSE`` failure from fields actually present.

    Responses and Chat Completions expose different optional metadata.  Read
    both through generic attribute/dict access and record observed values; do
    not infer a provider-specific cause from ``status``/``finish_reason``.
    """

    base = empty_response_failure()
    observed: list[str] = [f"transport={transport}"]

    def read(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    for key in ("status", "incomplete_details", "incomplete", "output_text"):
        rendered = _safe_observed_value(read(response, key))
        if rendered:
            observed.append(f"{key}={rendered}")
    rendered_finish = _safe_observed_value(finish_reason)
    if rendered_finish:
        observed.append(f"finish_reason={rendered_finish}")
    for key in ("index", "refusal"):
        rendered = _safe_observed_value(read(choice, key))
        if rendered:
            observed.append(f"choice.{key}={rendered}")
    if output_items is not None:
        item_types = [
            _safe_observed_value(
                item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            )
            for item in output_items
        ]
        item_types = [item_type for item_type in item_types if item_type]
        if item_types:
            observed.append(f"output_types={item_types[:16]}")
        else:
            observed.append("output_types=[]")
    return GenerationFailure(
        kind=base.kind,
        user_message=base.user_message,
        technical_detail=(
            "Assistant generation returned no response (empty final content); "
            + "; ".join(observed)
        ),
    )


# ツールループの既定上限。少なすぎるとモデルが仕事を終える前に打ち切られるため、
# 設定 `agentic_completion.max_tool_rounds` が無い場合はこの値を使う。
DEFAULT_MAX_TOOL_ROUNDS = 24


def _resolve_max_tool_rounds(explicit: int | None, config: Any | None) -> int:
    """ツールループ上限を解決する。明示値 > 設定 > 既定値 の順。"""
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            return DEFAULT_MAX_TOOL_ROUNDS

    value: Any = None
    if config is not None and hasattr(config, "get"):
        try:
            value = config.get("agentic_completion.max_tool_rounds", None)
        except Exception:  # noqa: BLE001
            value = None
        if value is None:
            try:
                section = config.get("agentic_completion", None)
            except Exception:  # noqa: BLE001
                section = None
            if isinstance(section, dict):
                value = section.get("max_tool_rounds")

    if value is None:
        return DEFAULT_MAX_TOOL_ROUNDS
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOOL_ROUNDS


def _new_native_turn_failure_breaker(
    config: Any | None,
) -> ToolFailureCircuitBreaker | None:
    """Create the one breaker shared by all routers in one native turn.

    Preserve RegistryToolRouter's existing opt-in semantics: ordinary chat
    remains unchanged, while Agent Team v3 delegation gets one breaker for
    the entire turn instead of one fresh breaker per provider round.
    """

    if config is None or not agent_team_v3_delegation_enabled(config):
        return None
    return ToolFailureCircuitBreaker(max_same_failure=2, failed_tool_budget=8)


def _observable_tool_arguments(
    registry: ToolRegistry,
    tool_name: str,
    arguments: Any,
) -> dict[str, Any]:
    """Return only arguments accepted by the canonical tool contract.

    Provider/model arguments are untrusted. Tool events and audit records must
    not publish unknown keys which RegistryToolRouter will reject, and
    compatibility aliases such as Docs ``project_id`` must be represented by
    their canonical key.
    """

    try:
        definition = registry.get(str(tool_name or ""))
    except Exception:
        definition = None
    if definition is None:
        return {}
    return _observable_arguments_for_definition(definition, arguments)


def _observable_arguments_for_definition(
    definition: ToolDefinition | None,
    arguments: Any,
) -> dict[str, Any]:
    """Normalize one trusted definition for provider/audit observations."""

    if definition is None:
        return {}
    normalizer = getattr(definition, "normalize_arguments", None)
    if not callable(normalizer):
        return {}
    try:
        normalized = normalizer(
            arguments if isinstance(arguments, dict) else {}
        )
    except Exception:
        # Observation must never leak the rejected raw arguments or interfere
        # with the router's authoritative failure result.
        return {}
    normalized = dict(normalized or {})
    # Hidden arguments are trusted wrapper inputs (for example a bound Story
    # conversation id), not provider-visible or audit-visible fields.
    for name in getattr(definition, "hidden_argument_names", ()) or ():
        normalized.pop(str(name), None)
    return normalized


class AgentTurnRunner:
    """Run a single AoiTalk agent turn with OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        provider_label: str = "openai",
        max_tool_rounds: int | None = None,
        max_tool_result_chars: int = 12000,
        config: Any | None = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.client = client
        self.provider_label = provider_label
        self.max_tool_rounds = _resolve_max_tool_rounds(max_tool_rounds, config)
        self.max_tool_result_chars = max_tool_result_chars
        self.config = config
        self.privacy_gateway = privacy_gateway or OutboundPrivacyGateway(
            config,
            session_id=session_id,
            user_id=user_id,
        )
        self.conversation_state_mode = "stateless"
        self.provider_state = ProviderState()
        self.prompt_cache_key: str | None = None
        self.prompt_cache_retention: str | None = None
        # reasoning summary を要求するか。非対応モデルで弾かれた時だけ False へ落とす。
        self.reasoning_summary_enabled = True
        self.context_budget = None
        # Keep the historical eager resolution for configured/probed local
        # runtimes, then refresh it with the actual AgentDefinition model at
        # turn start.  The refresh is essential for exact OpenAI registry
        # entries (the runner itself is constructed before an agent is chosen).
        self._refresh_context_budget(None)

    def _configured_max_output_tokens(self) -> int | None:
        """Read the native request's configured output-token limit."""

        if self.config is None:
            return None
        value: Any = None
        if hasattr(self.config, "get"):
            try:
                value = self.config.get("runtime.target_max_output_tokens", None)
            except Exception:  # noqa: BLE001
                value = None
        if value is None or not value:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        # Match the existing request builder's ``max(1, int(value))``
        # semantics exactly, including a truthy negative setting.
        return max(1, parsed)

    def _snapshot_response_tokens(
        self,
        request_kwargs: dict[str, Any] | None = None,
    ) -> int | None:
        """Return the output limit actually requested on the native wire.

        ``ContextBudget.response_tokens`` is intentionally a conservative
        prompt-budget reserve (historically capped at 4096).  The native
        request builder forwards its max-output field as configured, so
        snapshots inspect the already-built request and report that actual
        reservation without changing existing prompt-budget or wire semantics.
        """

        if isinstance(request_kwargs, dict):
            for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
                if key not in request_kwargs:
                    continue
                try:
                    return max(1, int(request_kwargs[key]))
                except (TypeError, ValueError, OverflowError):
                    break
        configured = self._configured_max_output_tokens()
        if configured is not None:
            return configured
        return self.context_budget.response_tokens if self.context_budget else None

    def _refresh_context_budget(self, model_name: str | None) -> None:
        """Resolve the active budget for the selected model.

        ``fallback`` is a conservative internal budget, not a claim about an
        OpenAI model's maximum.  Keep it out of snapshots so unknown OpenAI
        IDs continue to report an unknown window.
        """

        try:
            resolved_budget = resolve_context_budget(
                config=self.config,
                provider_key=self.provider_label,
                base_url=str(getattr(self.client, "base_url", "") or ""),
                model_name=model_name,
                api_key=getattr(self.client, "api_key", None),
                requested_max_tokens=self._configured_max_output_tokens(),
            )
            self.context_budget = (
                resolved_budget
                if resolved_budget.source != "fallback"
                else None
            )
        except Exception:
            self.context_budget = None

    def _model_egress_descriptor(
        self,
        *,
        transport: str,
        model: str | None = None,
    ) -> EgressDescriptor:
        """Describe a native provider request for the privacy transaction.

        ``OutboundPrivacyGateway.execute`` owns the protect/review/send
        transaction.  Keeping descriptor construction here means every
        native round (including tool-loop continuations and retries) carries
        the same auditable provider identity and destination.
        """

        base_url = str(getattr(self.client, "base_url", "") or "")
        return EgressDescriptor(
            action="model.generate",
            transport=transport,
            destination=base_url,
            provider=str(self.provider_label or ""),
            model=str(model or ""),
        )

    async def _execute_model_request(self, payload: dict[str, Any], **options: Any) -> Any:
        """Retry a broken connection once at the reviewed request boundary.

        Tool execution is outside this retry, so a failed follow-up cannot
        replay a committed tool. Keep SDK retries disabled and re-enter the
        privacy gateway for each attempt. Timeouts and HTTP/API rejections
        retain their existing failure semantics.
        """
        for attempt in range(2):
            try:
                return await self.privacy_gateway.execute(payload, **options)
            except APIConnectionError as error:
                cause = error.__cause__
                retry = attempt == 0 and not isinstance(error, APITimeoutError)
                logger.warning(
                    "Native model transport failed: provider=%s model=%s "
                    "exception_type=%s cause_type=%s retry=%s",
                    self.provider_label,
                    options.get("model", ""),
                    type(error).__name__,
                    type(cause).__name__ if cause is not None else "none",
                    retry,
                )
                if not retry:
                    raise
                await asyncio.sleep(0.5)

    async def run(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
        cloud_advisor_origin: Any | None = None,
        cloud_advisor_assessment: Any | None = None,
    ) -> NativeRunResult:
        """Run one turn, optionally resolving tools before each model round.

        ``load_tool_pack`` changes the effective tool set during a turn.  A
        caller that owns the session (the normal ``AgentLLMClient`` path)
        can provide a resolver so the next model request receives the newly
        loaded function schemas.  Direct callers retain the historical static
        ``agent.tools`` behavior when no resolver is supplied.
        """
        # Help is a trusted, Guide-only controller turn.  Enforce the
        # stateless boundary here as a final guard for direct native callers
        # as well as the normal AgentLLMClient path, then restore the ordinary
        # provider state even when transport or tool execution fails.
        isolated_state: tuple[Any, Any, Any, Any, Any] | None = None
        isolated_privacy_gateway: OutboundPrivacyGateway | None = None
        isolated_privacy_gateway_previous: OutboundPrivacyGateway | None = None
        from ..services.turn_context import (
            AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
            get_turn_context,
        )

        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        effective_agent = (
            replace(
                agent,
                instructions=AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
            )
            if suppress_automatic_context
            else agent
        )
        if suppress_automatic_context:
            isolated_state = (
                self.conversation_state_mode,
                self.provider_state,
                self.prompt_cache_key,
                self.prompt_cache_retention,
                self.reasoning_summary_enabled,
            )
            self.conversation_state_mode = "stateless"
            self.provider_state = ProviderState(mode="stateless")
            self.prompt_cache_key = None
            self.prompt_cache_retention = None
            # Direct AgentTurnRunner callers do not pass through TerminalMode's
            # provider snapshot.  Give the controller turn a fresh alias scope
            # so a prior ordinary request can never restore an old secret-shaped
            # marker in the Guide answer.  Restore the exact gateway object on
            # every exit, including transport/tool failures.
            isolated_privacy_gateway_previous = self.privacy_gateway
            try:
                turn = get_turn_context()
                isolated_privacy_gateway = OutboundPrivacyGateway(
                    self.config,
                    session_id=(
                        str(getattr(turn, "session_id", None) or "")
                        or str(getattr(isolated_privacy_gateway_previous, "session_id", "") or "")
                    ),
                    user_id=(
                        str(getattr(turn, "user_id", None) or "")
                        or str(getattr(isolated_privacy_gateway_previous, "user_id", "") or "")
                    ),
                    session_context={},
                    project_metadata={},
                )
                self.privacy_gateway = isolated_privacy_gateway
            except Exception as exc:
                # A Help turn must never fall back to the ordinary gateway.  A
                # construction failure would otherwise re-enable aliases or
                # outbound policy inherited from the preceding user turn.
                raise RuntimeError(
                    "AoiTalk Help isolated privacy gateway is unavailable"
                ) from exc

        try:
            # AgentTurnRunner is created before the per-turn AgentDefinition is
            # assembled.  Resolve again here so the official model registry sees
            # the model that will actually be sent over the wire.
            self._refresh_context_budget(effective_agent.model)
            # Bind the parent-owned Cloud Advisor metadata only for the duration of
            # this native turn.  The default scope is Main-agent + empty semantic
            # assessment, so existing callers remain unchanged and automatic mode
            # cannot escalate from input length/regexes/model-controlled flags.
            with cloud_advisor_parent_scope(
                origin=cloud_advisor_origin,
                assessment=cloud_advisor_assessment,
            ):
                return await self._run_core(
                    effective_agent,
                    user_input,
                    stream_callback=stream_callback,
                    tools_provider=tools_provider,
                )
        finally:
            if isolated_privacy_gateway is not None:
                self.privacy_gateway = isolated_privacy_gateway_previous
            if isolated_state is not None:
                (
                    self.conversation_state_mode,
                    self.provider_state,
                    self.prompt_cache_key,
                    self.prompt_cache_retention,
                    self.reasoning_summary_enabled,
                ) = isolated_state

    async def _run_core(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
    ) -> NativeRunResult:
        # local_only changes the execution deployment, not just the payload.
        # Resolve the configured privacy sidecar explicitly and fail closed
        # when no trusted local model is available; never fall back to cloud.
        current_base_url = str(getattr(self.client, "base_url", "") or "")
        if (
            self.privacy_gateway.mode == "local_only"
            and self.privacy_gateway.provider_class(
                self.provider_label, current_base_url
            )
            != "local"
        ):
            return await self._run_local_only(
                agent,
                user_input,
                stream_callback=stream_callback,
                tools_provider=tools_provider,
            )
        # 公式 OpenAI 経路は Responses API を使う。openrouter など base_url を差し替えた
        # OpenAI 互換プロバイダは従来の chat.completions を維持する。
        if self.provider_label == "openai":
            return await self._run_core_responses(
                agent,
                user_input,
                stream_callback=stream_callback,
                tools_provider=tools_provider,
            )
        return await self._run_core_chat(
            agent,
            user_input,
            stream_callback=stream_callback,
            tools_provider=tools_provider,
        )

    async def _run_core_chat(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
    ) -> NativeRunResult:
        await _emit(stream_callback, "stream_start", {"message": "応答を生成しています"})
        suppress_automatic_context = _help_turn_is_isolated()

        seed_messages = (
            _prompt_messages_for_isolated_help(user_input)
            if suppress_automatic_context
            else _prompt_messages_or_user(user_input)
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.instructions or ""},
            *seed_messages,
        ]
        plain_user_input = prompt_text(
            seed_messages[-1].get("content") if seed_messages else ""
        ) if suppress_automatic_context else prompt_text(user_input)
        tool_records: list[ToolExecutionRecord] = []
        usage_records: list[dict[str, int]] = []
        context_snapshots: list[dict[str, Any]] = []
        active_tools = _resolve_runtime_tools(agent, tools_provider)
        tools_payload = _tool_specs(active_tools)
        requested_tool_choice = _normalize_tool_choice(
            agent.model_settings.tool_choice,
            has_tools=bool(tools_payload),
        )
        current_tool_choice = requested_tool_choice
        final_output = ""
        failure_breaker = _new_native_turn_failure_breaker(self.config)
        hard_stop = False
        authoritative_task_complete = False

        for round_index in range(self.max_tool_rounds + 1):
            # A tool call can load a deferred pack.  Resolve the effective
            # registry and schemas again before the next provider request so
            # the newly loaded tools are callable as native functions.
            if round_index:
                active_tools = _resolve_runtime_tools(agent, tools_provider)
                tools_payload = _tool_specs(active_tools)
                if authoritative_task_complete:
                    active_tools = []
                    tools_payload = []
                    current_tool_choice = None
            approved_directive = (
                None if suppress_automatic_context else get_approved_action_directive()
            )
            while approved_directive is not None:
                existing_receipt = await get_approved_action_receipt(
                    approved_directive
                )
                if existing_receipt is None:
                    break
                receipt_output = str(existing_receipt.get("result") or "")
                receipt_arguments = dict(existing_receipt.get("arguments") or {})
                receipt_definition = next(
                    (
                        tool
                        for tool in active_tools
                        if tool.name == approved_directive.tool
                    ),
                    None,
                )
                observed_receipt_arguments = _observable_arguments_for_definition(
                    receipt_definition,
                    receipt_arguments,
                )
                provider_call_id = approved_directive.call_id
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": provider_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": approved_directive.tool,
                                        "arguments": json.dumps(
                                            observed_receipt_arguments,
                                            ensure_ascii=False,
                                            sort_keys=True,
                                        ),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": provider_call_id,
                            "content": receipt_output,
                        },
                    ]
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool=approved_directive.tool,
                        arguments=observed_receipt_arguments,
                        result=receipt_output,
                    )
                )
                await accept_approved_action_receipt(
                    approved_directive,
                    existing_receipt,
                )
                approved_directive = (
                    None
                    if suppress_automatic_context
                    else get_approved_action_directive()
                )

            planning_state = (
                None
                if suppress_automatic_context
                else get_current_planning_run_state()
            )
            if planning_state is not None and planning_state.phase == PlanningRunPhase.COMPLETED:
                active_tools = []
                tools_payload = []
                current_tool_choice = None
            elif approved_directive is not None:
                approved_tools = [
                    tool for tool in active_tools if tool.name == approved_directive.tool
                ]
                if len(approved_tools) != 1:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_tool_unavailable",
                    )
                active_tools = approved_tools
                tools_payload = _tool_specs(active_tools)
                current_tool_choice = "required"
            tool_registry = ToolRegistry()
            for tool in active_tools:
                tool_registry.register(tool)
            tool_router = RegistryToolRouter(
                tool_registry,
                log_prefix=f"NativeAgentTurnRunner:{agent.name}",
                config=self.config,
                # A delegated specialist runs inside the parent task's
                # contextvar scope.  Pass its own request explicitly so the
                # root user's role-routing words (for example
                # ``Docs操作エージェント``) do not leak into the child and
                # block the specialist's direct Docs tools.
                user_input=plain_user_input,
                failure_breaker=failure_breaker,
            )
            kwargs = self._build_completion_kwargs(
                agent=agent,
                messages=messages,
                tools_payload=tools_payload,
                tool_choice=current_tool_choice,
            )
            observed_messages = kwargs.get("messages", [])
            excluded_texts = list(
                getattr(self, "snapshot_excluded_texts", []) or []
            )
            if not excluded_texts:
                excluded_texts = [
                    getattr(self, "snapshot_rendered_bundle", "")
                ]
            for excluded_text in excluded_texts:
                observed_messages = without_text_from_last_role(
                    observed_messages,
                    excluded_text,
                    role="user",
                )
            request_snapshot = None
            try:
                request_snapshot = snapshot(
                    provider=self.provider_label,
                    model=agent.model,
                    components=[
                        *message_components(observed_messages),
                        *list(getattr(self, "snapshot_bundle_components", []) or []),
                        *list(getattr(self, "snapshot_dynamic_components", []) or []),
                        *tool_components(kwargs.get("tools", []), source="chat.completions tools payload"),
                    ],
                    request_index=len(context_snapshots),
                    request_kind="chat.completions",
                    context_window_tokens=self.context_budget.context_window_tokens if self.context_budget else None,
                    response_tokens=self._snapshot_response_tokens(kwargs),
                    window_source=self.context_budget.source if self.context_budget else None,
                )
                context_snapshots.append(request_snapshot)
            except Exception:
                logger.warning(
                    "chat.completions context observation failed; continuing",
                    exc_info=True,
                )
            base_url = str(getattr(self.client, "base_url", "") or "")
            descriptor = self._model_egress_descriptor(
                transport="openai.chat.completions",
                model=agent.model,
            )

            async def send_chat_request(outbound_kwargs: dict[str, Any]) -> Any:
                # The sender is intentionally nested inside the gateway
                # transaction.  It can therefore receive only the final
                # reviewed payload and cannot accidentally send ``kwargs``
                # captured before redaction.
                return await self.client.chat.completions.create(**outbound_kwargs)

            try:
                response = await self._execute_model_request(
                    kwargs,
                    provider=self.provider_label,
                    descriptor=descriptor,
                    sender=send_chat_request,
                    base_url=base_url,
                    source_kind="model_request",
                    model=agent.model,
                )
            except Exception as error:
                if not tool_records and not usage_records:
                    raise
                failure = classify_generation_error(error)
                await _emit(stream_callback, "stream_end", {"content": ""})
                return NativeRunResult(
                    final_output="",
                    messages=list(messages),
                    tool_calls=list(tool_records),
                    usage_records=list(usage_records),
                    context_snapshots=list(context_snapshots),
                    generation_failure=failure,
                )
            usage = _normalized_usage(
                getattr(response, "usage", None),
                provider=self.provider_label,
                resolved_model=getattr(response, "model", None),
            )
            if usage:
                usage_records.append(usage)
                if request_snapshot is not None:
                    context_snapshots[-1] = reconcile_snapshot(
                        request_snapshot,
                        usage.get("input_tokens"),
                    )
            choice = response.choices[0]
            message = choice.message
            thinking_text = thinking_text_from_message(message)
            if thinking_text:
                await _emit(
                    stream_callback,
                    "thinking",
                    {"text": thinking_text, "kind": "raw", "round": round_index},
                )
            content = str(getattr(message, "content", "") or "")
            final_output = content
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            assistant_payload = _assistant_message_payload(
                message,
                registry=tool_registry,
            )

            if approved_directive is not None:
                if not tool_calls:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_plain_final_rejected",
                    )
                if len(tool_calls) != 1:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_multiple_calls_rejected",
                    )

            if not tool_calls:
                if not content.strip():
                    failure = _empty_response_failure(
                        transport="chat.completions",
                        response=response,
                        choice=choice,
                        finish_reason=getattr(choice, "finish_reason", None),
                    )
                    partial_result = NativeRunResult(
                        final_output="",
                        messages=[*messages, assistant_payload],
                        tool_calls=list(tool_records),
                        usage_records=list(usage_records),
                        context_snapshots=list(context_snapshots),
                        generation_failure=failure,
                    )
                    await _emit(stream_callback, "stream_end", {"content": ""})
                    return partial_result
                if content:
                    content = str(self.privacy_gateway.restore_aliases(content))
                    await _emit(stream_callback, "stream_token", {"content": content})
                await _emit(stream_callback, "stream_end", {"content": content})
                result = NativeRunResult(
                    final_output=content,
                    messages=[
                        *messages,
                        assistant_payload,
                    ],
                    tool_calls=list(tool_records),
                    usage_records=list(usage_records),
                    context_snapshots=list(context_snapshots),
                )
                return result

            messages.append(assistant_payload)
            if content:
                # ツール呼び出しを伴うラウンドの通常テキストは途中経過として配信する。
                # 最終ラウンドは stream_token 側で配信するためここでは発行しない。
                await _emit(
                    stream_callback,
                    "assistant_text",
                    {"text": content, "round": round_index},
                )
            for tool_call_index, tool_call in enumerate(tool_calls):
                tool_name = _tool_call_name(tool_call)
                observable_name = observable_tool_name(tool_registry, tool_name)
                provider_call_id = _tool_call_id(tool_call)
                call_id = (
                    approved_directive.call_id
                    if approved_directive is not None
                    else provider_call_id
                )
                args, parse_error = _tool_call_arguments(tool_call)
                if not parse_error and approved_directive is not None:
                    args = self.privacy_gateway.restore_tool_arguments(
                        args,
                        tool_name=tool_name,
                    )
                if approved_directive is not None:
                    if parse_error:
                        await fail_approved_action(
                            approved_directive,
                            reason="approved_action_arguments_invalid",
                            detail=parse_error,
                        )
                    try:
                        args = bind_approved_action_call(
                            approved_directive,
                            tool_name=tool_name,
                            proposed_arguments=args,
                        )
                    except PlanningInteractionTerminated as exc:
                        await fail_approved_action(
                            approved_directive,
                            reason=exc.reason,
                        )
                event_args = (
                    {}
                    if parse_error
                    else _observable_tool_arguments(
                        tool_registry, tool_name, args
                    )
                )
                await _emit(
                    stream_callback,
                    "tool_start",
                    {
                        "tool": observable_name,
                        "tool_args": event_args,
                        "operation_id": call_id,
                        "tool_call_id": call_id,
                        "message": f"{observable_name} を実行しています",
                    },
                )

                execution_success = False
                execution_error = parse_error or ""
                audit_args = dict(event_args)
                if parse_error:
                    result_text = f"Error: invalid JSON arguments: {parse_error}"
                elif _simple_task_unexpected_mutation_execution_blocked(
                    plain_user_input,
                    tool_name,
                    tool_records,
                ):
                    hard_stop = True
                    result_text = json.dumps(
                        {
                            "success": False,
                            "error_code": "unexpected_task_mutation",
                            "error": (
                                "simple task creation permits only duplicate search "
                                "followed by create_task"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    execution_error = "unexpected_task_mutation"
                elif (
                    tool_name == "create_task"
                    and not _simple_task_create_execution_allowed(
                        plain_user_input,
                        tool_records,
                    )
                ):
                    hard_stop = True
                    result_text = json.dumps(
                        {
                            "success": False,
                            "error_code": "duplicate_search_required",
                            "error": (
                                "create_task requires a successful, unambiguous "
                                "empty search_task_candidates result first"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    execution_error = "duplicate_search_required"
                else:
                    if approved_directive is None:
                        args = self.privacy_gateway.restore_tool_arguments(
                            args,
                            tool_name=tool_name,
                        )
                    tool_result = await tool_router.execute_async(
                        UnifiedToolCall(
                            tool=tool_name,
                            arguments=args,
                            call_id=call_id,
                        )
                    )
                    result_text = tool_result.model_output
                    execution_success = tool_result.success
                    execution_error = tool_result.error or ""
                    audit_args = _observable_tool_arguments(
                        tool_registry,
                        tool_name,
                        tool_result.call.arguments,
                    )

                model_payload = model_tool_result_payload(
                    tool_name=observable_name,
                    output=result_text,
                    user_input=plain_user_input,
                    max_chars=self.max_tool_result_chars,
                    config=self.config,
                    legacy_clip=_clip_text,
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool=observable_name,
                        arguments=dict(audit_args),
                        result=result_text,
                    )
                )
                if tool_name == "create_task" and _simple_task_request_is_explicit(
                    plain_user_input
                ):
                    if (
                        simple_task_mutation_completion_state(
                            plain_user_input,
                            tool_records,
                        )
                        is SimpleTaskMutationCompletionState.COMPLETE
                    ):
                        authoritative_task_complete = True
                    else:
                        # A create that did not produce an authoritative
                        # receipt is terminal for this native attempt.  Do
                        # not retry a potentially committed mutation and risk
                        # a duplicate row; the outer controller will surface
                        # the fail-closed result.
                        hard_stop = True
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": provider_call_id,
                        "content": model_payload.text,
                    }
                )
                await _emit(
                    stream_callback,
                    "tool_end",
                    {
                        "tool": observable_name,
                        "tool_args": audit_args,
                        "operation_id": call_id,
                        "tool_call_id": call_id,
                        "tool_result": {
                            "tool": observable_name,
                            "arguments": audit_args,
                            "output": result_text,
                            "error": None if execution_success else (execution_error or result_text),
                            "mutation_confirmed": bool(
                                approved_directive is not None and execution_success
                            ),
                            "tool_call_id": call_id,
                        },
                        "message": "ツール実行が完了しました",
                    },
                )

                if approved_directive is not None:
                    await persist_and_accept_approved_action_result(
                        approved_directive,
                        arguments=audit_args,
                        result=result_text,
                        success=execution_success,
                    )
                elif (
                    tool_name == "submit_plan_for_approval"
                    and (state := get_current_planning_run_state()) is not None
                    and state.phase == PlanningRunPhase.EXECUTING
                ):
                    # Provider batches are proposals, not authority.  Once an
                    # approval response activates execution, every remaining
                    # call in that same batch is acknowledged as not executed;
                    # the next provider round receives only action[0].
                    for skipped in tool_calls[tool_call_index + 1 :]:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _tool_call_id(skipped),
                                "content": (
                                    "Not executed: approved actions start on the next "
                                    "server-controlled round."
                                ),
                            }
                        )
                    break

                if hard_stop or authoritative_task_complete:
                    for skipped in tool_calls[tool_call_index + 1 :]:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _tool_call_id(skipped),
                                "content": (
                                    "Not executed: the deterministic task mutation "
                                    "was already terminal (or duplicate search failed)."
                                ),
                            }
                        )
                    break

            if round_index == 0 and current_tool_choice == "required":
                current_tool_choice = "auto"
            if hard_stop:
                break

        fallback = str(
            self.privacy_gateway.restore_aliases(
                final_output or "ツール実行後の最終応答を生成できませんでした。"
            )
        )
        await _emit(stream_callback, "stream_token", {"content": fallback})
        await _emit(stream_callback, "stream_end", {"content": fallback})
        logger.warning("Native agent turn exceeded max tool rounds: %s", agent.name)
        result = NativeRunResult(
            final_output=fallback,
            messages=[
                *messages,
                {"role": "assistant", "content": fallback},
            ],
            tool_calls=list(tool_records),
            usage_records=list(usage_records),
            context_snapshots=list(context_snapshots),
            tool_rounds_exhausted=True,
        )
        return result

    async def _run_local_only(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
    ) -> NativeRunResult:
        settings = self.privacy_gateway.settings
        provider = settings.local_provider
        base_url = ""
        api_key = ""
        if self.config is not None and hasattr(self.config, "get"):
            base_url = str(
                self.config.get(f"{provider}.base_url", "")
                or self.config.get("sglang_base_url", "")
                or ""
            )
            api_key = str(self.config.get(f"{provider}.api_key", "") or "")
        if not base_url:
            import os as _os

            env_names = {
                "ollama": "OLLAMA_BASE_URL",
                "sglang": "SGLANG_BASE_URL",
                "openai_compatible_local": "OPENAI_COMPATIBLE_LOCAL_BASE_URL",
            }
            base_url = _os.getenv(env_names.get(provider, ""), "")
        if not base_url:
            if provider == "ollama":
                base_url = "http://127.0.0.1:11434/v1"
            elif provider == "sglang":
                base_url = "http://127.0.0.1:30000/v1"
            else:
                raise ExternalProviderBlocked(
                    "local_only requires a configured trusted local privacy model endpoint"
                )
        if (
            provider_class := self.privacy_gateway.provider_class(provider, base_url)
        ) != "local":
            raise ExternalProviderBlocked(
                f"privacy local endpoint is not trusted ({provider}: {base_url})"
            )
        model = settings.local_model
        if not model and self.config is not None and hasattr(self.config, "get"):
            model = str(self.config.get(f"{provider}.model", "") or "")
        if not model:
            raise ExternalProviderBlocked(
                "local_only requires external_model_privacy.local_model or a configured local model"
            )
        local_agent = AgentDefinition(
            name=agent.name,
            instructions=agent.instructions,
            model=model,
            tools=agent.tools,
            model_settings=agent.model_settings,
        )
        local_runner = AgentTurnRunner(
            client=create_async_openai_client(api_key=api_key, base_url=base_url),
            provider_label=provider,
            max_tool_rounds=self.max_tool_rounds,
            max_tool_result_chars=self.max_tool_result_chars,
            config=self.config,
            privacy_gateway=self.privacy_gateway,
            session_id=self.privacy_gateway.session_id,
            user_id=self.privacy_gateway.user_id,
        )
        return await local_runner.run(
            local_agent,
            user_input,
            stream_callback=stream_callback,
            tools_provider=tools_provider,
        )

    async def _run_core_responses(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
    ) -> NativeRunResult:
        """公式 OpenAI Responses API でエージェントターンを実行する。

        gpt-5.6-terra 系は chat.completions では function tools と reasoning_effort を
        併用できないため、function tools を使う経路はすべて Responses API に寄せる。
        会話状態はサーバー保存に依存せず、毎ラウンド input を全量送信する（store=False）。
        """
        suppress_automatic_context = _help_turn_is_isolated()
        await _emit(stream_callback, "stream_start", {"message": "応答を生成しています"})

        # Responses API へ渡す input items（毎ラウンド全量送信する状態）。
        seed_messages = (
            _prompt_messages_for_isolated_help(user_input)
            if suppress_automatic_context
            else _prompt_messages_or_user(user_input)
        )
        plain_user_input = (
            prompt_text(seed_messages[-1].get("content") if seed_messages else "")
            if suppress_automatic_context
            else prompt_text(user_input)
        )
        request_seed_messages = seed_messages
        if (
            self.conversation_state_mode == "provider-managed"
            and self.provider_state.previous_response_id
        ):
            request_seed_messages = [
                message
                for message in reversed(seed_messages)
                if message.get("role") == "user"
            ][:1]
            request_seed_messages.reverse()
        input_items: list[dict[str, Any]] = _responses_input_items(request_seed_messages)
        stateless_input_items: list[dict[str, Any]] = list(
            _responses_input_items(seed_messages)
        )
        # 外部消費用の従来 chat 形式メッセージ履歴。
        chat_messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.instructions or ""},
            *seed_messages,
        ]

        tool_records: list[ToolExecutionRecord] = []
        usage_records: list[dict[str, int]] = []
        context_snapshots: list[dict[str, Any]] = []
        active_tools = _resolve_runtime_tools(agent, tools_provider)
        tools_payload = _responses_tool_specs(active_tools)
        requested_tool_choice = _normalize_tool_choice(
            agent.model_settings.tool_choice,
            has_tools=bool(tools_payload),
        )
        current_tool_choice = requested_tool_choice
        effort = getattr(agent.model_settings.reasoning, "effort", None)
        final_output = ""
        failure_breaker = _new_native_turn_failure_breaker(self.config)
        hard_stop = False
        authoritative_task_complete = False

        def build_request_snapshot(
            request_kwargs: dict[str, Any],
            *,
            request_kind: str,
            include_provider_context: bool = True,
        ) -> dict[str, Any]:
            request_input = request_kwargs.get("input") or []
            snapshot_excluded_texts = list(
                getattr(self, "snapshot_excluded_texts", []) or []
            )

            injected_on_wire = any(
                text
                and last_role_contains_text(
                    request_input,
                    text,
                    role="user",
                )
                for text in snapshot_excluded_texts
            )
            observed_input = request_input
            if isinstance(request_input, list):
                excluded_texts = list(snapshot_excluded_texts)
                if not excluded_texts:
                    excluded_texts = [
                        getattr(self, "snapshot_rendered_bundle", "")
                    ]
                for excluded_text in excluded_texts:
                    observed_input = without_text_from_last_role(
                        observed_input,
                        excluded_text,
                        role="user",
                    )
            request_components = [
                component(
                    "system_instructions",
                    "System instructions",
                    request_kwargs.get("instructions"),
                    source="responses instructions",
                ),
            ]
            observed_items = (
                observed_input
                if isinstance(observed_input, list)
                else [observed_input]
            )
            user_indices = [
                index
                for index, item in enumerate(observed_items)
                if str(_responses_item_get(item, "role") or "") == "user"
            ]
            last_user_index = user_indices[-1] if user_indices else None
            for index, item in enumerate(observed_items):
                item_type = _responses_item_type(item)
                role = str(_responses_item_get(item, "role") or "")
                if item_type == "function_call_output":
                    category, label = "tool_results", "Tool results"
                elif role == "user" and index == last_user_index:
                    category, label = (
                        "current_user_message",
                        "Current user message",
                    )
                else:
                    category, label = (
                        "conversation_history",
                        "Conversation history",
                    )
                request_components.append(
                    component(
                        category,
                        label,
                        item,
                        source=f"responses input[{index}]",
                    )
                )
                if _responses_item_contains_image(item):
                    request_components.append(
                        component(
                            "attachments",
                            "添付ファイル・画像由来の入力",
                            source=f"responses input[{index}] image parts",
                            measurement="unavailable",
                            preview="画像入力（バイナリ・URLは保存しません）",
                        )
                    )
            request_components.extend(
                [
                    *(
                        list(
                            getattr(
                                self,
                                "snapshot_bundle_components",
                                [],
                            )
                            or []
                        )
                        if injected_on_wire
                        else []
                    ),
                    *(
                        list(
                            getattr(
                                self,
                                "snapshot_dynamic_components",
                                [],
                            )
                            or []
                        )
                        if injected_on_wire
                        else []
                    ),
                    *tool_components(
                        request_kwargs.get("tools", []),
                        source="responses tools payload",
                    ),
                ]
            )
            if (
                include_provider_context
                and "previous_response_id" in request_kwargs
            ):
                request_components.append(
                    component(
                        "provider_managed",
                        "Provider-managed context",
                        source="responses encrypted reasoning",
                        measurement="unavailable",
                        preview="暗号化されたreasoning itemの詳細は取得不能",
                    )
                )
            return snapshot(
                provider=self.provider_label,
                model=agent.model,
                components=request_components,
                request_index=len(context_snapshots),
                request_kind=request_kind,
                context_window_tokens=(
                    self.context_budget.context_window_tokens
                    if self.context_budget
                    else None
                ),
                response_tokens=self._snapshot_response_tokens(request_kwargs),
                window_source=self.context_budget.source if self.context_budget else None,
            )

        def safe_build_request_snapshot(
            request_kwargs: dict[str, Any],
            **options: Any,
        ) -> dict[str, Any] | None:
            try:
                return build_request_snapshot(request_kwargs, **options)
            except Exception:
                logger.warning(
                    "responses context observation failed; continuing",
                    exc_info=True,
                )
                return None

        async def create_response(request_kwargs: dict[str, Any]) -> Any:
            """Responses API を呼ぶ。summary 非対応モデルだけ 1 回だけ外して再試行する。"""
            base_url = str(getattr(self.client, "base_url", "") or "")
            descriptor = self._model_egress_descriptor(
                transport="openai.responses",
                model=agent.model,
            )

            async def send_response_request(outbound_kwargs: dict[str, Any]) -> Any:
                # Keep the provider sender inside the privacy transaction so
                # approval/redaction is applied to the exact request sent.
                return await self.client.responses.create(**outbound_kwargs)

            try:
                return await self._execute_model_request(
                    request_kwargs,
                    provider=self.provider_label,
                    descriptor=descriptor,
                    sender=send_response_request,
                    base_url=base_url,
                    source_kind="model_request",
                    model=agent.model,
                )
            except Exception as exc:  # noqa: BLE001
                if not (
                    self.reasoning_summary_enabled
                    and _has_reasoning_summary(request_kwargs)
                    and _is_reasoning_summary_error(exc)
                ):
                    raise
                # 以降のラウンドでも summary を付けないようランナー単位で無効化する。
                self.reasoning_summary_enabled = False
                logger.warning(
                    "reasoning summary 非対応のため summary なしで再試行します: %s",
                    agent.model,
                )
                retry_kwargs = _without_reasoning_summary(request_kwargs)
                return await self._execute_model_request(
                    retry_kwargs,
                    provider=self.provider_label,
                    descriptor=descriptor,
                    sender=send_response_request,
                    base_url=str(getattr(self.client, "base_url", "") or ""),
                    source_kind="model_request",
                    model=agent.model,
                )

        for round_index in range(self.max_tool_rounds + 1):
            # See the chat-completions path above.  Keep the provider payload
            # and execution registry in lockstep with the session's current
            # deferred-pack state on every round.
            if round_index:
                active_tools = _resolve_runtime_tools(agent, tools_provider)
                tools_payload = _responses_tool_specs(active_tools)
                if authoritative_task_complete:
                    active_tools = []
                    tools_payload = []
                    current_tool_choice = None
            approved_directive = (
                None if suppress_automatic_context else get_approved_action_directive()
            )
            while approved_directive is not None:
                existing_receipt = await get_approved_action_receipt(
                    approved_directive
                )
                if existing_receipt is None:
                    break
                receipt_output = str(existing_receipt.get("result") or "")
                receipt_arguments = dict(existing_receipt.get("arguments") or {})
                receipt_definition = next(
                    (
                        tool
                        for tool in active_tools
                        if tool.name == approved_directive.tool
                    ),
                    None,
                )
                observed_receipt_arguments = _observable_arguments_for_definition(
                    receipt_definition,
                    receipt_arguments,
                )
                synthetic_call = {
                    "type": "function_call",
                    "id": f"fc_{approved_directive.call_id}",
                    "call_id": approved_directive.call_id,
                    "name": approved_directive.tool,
                    "arguments": json.dumps(
                        observed_receipt_arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
                synthetic_output = {
                    "type": "function_call_output",
                    "call_id": approved_directive.call_id,
                    "output": receipt_output,
                }
                input_items.extend([synthetic_call, synthetic_output])
                stateless_input_items.extend([synthetic_call, synthetic_output])
                chat_messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": approved_directive.call_id,
                                    "type": "function",
                                    "function": {
                                        "name": approved_directive.tool,
                                        "arguments": synthetic_call["arguments"],
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": approved_directive.call_id,
                            "content": receipt_output,
                        },
                    ]
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool=approved_directive.tool,
                        arguments=observed_receipt_arguments,
                        result=receipt_output,
                    )
                )
                await accept_approved_action_receipt(
                    approved_directive,
                    existing_receipt,
                )
                approved_directive = (
                    None
                    if suppress_automatic_context
                    else get_approved_action_directive()
                )

            planning_state = (
                None
                if suppress_automatic_context
                else get_current_planning_run_state()
            )
            if planning_state is not None and planning_state.phase == PlanningRunPhase.COMPLETED:
                active_tools = []
                tools_payload = []
                current_tool_choice = None
            elif approved_directive is not None:
                approved_tools = [
                    tool for tool in active_tools if tool.name == approved_directive.tool
                ]
                if len(approved_tools) != 1:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_tool_unavailable",
                    )
                active_tools = approved_tools
                tools_payload = _responses_tool_specs(active_tools)
                current_tool_choice = "required"
            tool_registry = ToolRegistry()
            for tool in active_tools:
                tool_registry.register(tool)
            tool_router = RegistryToolRouter(
                tool_registry,
                log_prefix=f"NativeAgentTurnRunner:{agent.name}",
                config=self.config,
                user_input=plain_user_input,
                failure_breaker=failure_breaker,
            )
            kwargs: dict[str, Any] = {
                "model": agent.model,
                "input": input_items,
                "instructions": agent.instructions or "",
                "store": self.conversation_state_mode == "provider-managed",
                # store=False では reasoning item を ID 参照で再送できないため、
                # encrypted_content を受け取って次ラウンドの input に含める。
                "include": ["reasoning.encrypted_content"],
            }
            if tools_payload:
                kwargs["tools"] = tools_payload
            if current_tool_choice:
                kwargs["tool_choice"] = current_tool_choice
            if effort:
                reasoning_kwargs: dict[str, Any] = {"effort": effort}
                if self.reasoning_summary_enabled:
                    # 推論サマリーを受け取り thinking イベントとして配信する。
                    reasoning_kwargs["summary"] = "auto"
                kwargs["reasoning"] = reasoning_kwargs
            if self.config and hasattr(self.config, "get"):
                max_output_tokens = self.config.get(
                    "runtime.target_max_output_tokens", None
                )
                if max_output_tokens:
                    kwargs["max_output_tokens"] = max(1, int(max_output_tokens))
            if self.prompt_cache_key:
                kwargs["prompt_cache_key"] = self.prompt_cache_key
            if self.prompt_cache_retention:
                kwargs["prompt_cache_retention"] = self.prompt_cache_retention

            previous_response_id = self.provider_state.previous_response_id
            if (
                self.conversation_state_mode == "provider-managed"
                and previous_response_id
            ):
                kwargs["previous_response_id"] = previous_response_id

            request_snapshot = safe_build_request_snapshot(
                kwargs,
                request_kind="responses",
            )
            if request_snapshot is not None:
                context_snapshots.append(request_snapshot)

            async def _partial_result_after_provider_failure(
                error: Exception,
            ) -> NativeRunResult:
                """Preserve tool evidence and usage when a follow-up fails.

                Reads and arbitrary mutations are evidence too. Dropping them
                hides partial work and can cause a subsequent retry to repeat
                an already committed operation. The failure marker prevents
                this partial result from being reported as a completed turn.
                Turns with no evidence retain the historical exception path.
                """

                if not tool_records and not usage_records:
                    raise error
                failure = classify_generation_error(error)
                await _emit(stream_callback, "stream_end", {"content": ""})
                return NativeRunResult(
                    final_output="",
                    messages=list(chat_messages),
                    tool_calls=list(tool_records),
                    usage_records=list(usage_records),
                    context_snapshots=list(context_snapshots),
                    generation_failure=failure,
                )

            try:
                response = await create_response(kwargs)
            except Exception as first_error:
                if authoritative_task_complete and tool_records:
                    # Once a durable deterministic create is proven, a
                    # provider-managed finalization failure is not a reason
                    # to spend another request rebuilding context.  Invalidate
                    # the managed response id for the next turn, then let the
                    # controller finalize from the receipt.
                    if self.conversation_state_mode == "provider-managed":
                        self.conversation_state_mode = "stateless"
                        self.provider_state.reset()
                    return await _partial_result_after_provider_failure(first_error)
                if self.conversation_state_mode != "provider-managed":
                    return await _partial_result_after_provider_failure(first_error)
                # Provider state may expire or be unavailable after a server
                # restart.  Rebuild from AoiTalk's canonical transcript.
                logger.warning("provider-managed stateを破棄してstatelessへフォールバック", exc_info=True)
                self.conversation_state_mode = "stateless"
                self.provider_state.reset()
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("previous_response_id", None)
                retry_kwargs["store"] = False
                retry_kwargs["input"] = list(stateless_input_items)
                request_snapshot = safe_build_request_snapshot(
                    retry_kwargs,
                    request_kind="responses.stateless_retry",
                    include_provider_context=False,
                )
                if request_snapshot is not None:
                    context_snapshots.append(request_snapshot)
                try:
                    response = await create_response(retry_kwargs)
                except Exception as retry_error:
                    return await _partial_result_after_provider_failure(retry_error)
            usage = _normalized_usage(
                getattr(response, "usage", None),
                provider=self.provider_label,
                resolved_model=getattr(response, "model", None),
            )
            if usage:
                usage_records.append(usage)
                if request_snapshot is not None:
                    context_snapshots[-1] = reconcile_snapshot(
                        request_snapshot,
                        usage.get("input_tokens"),
                    )
            output_items = list(_responses_item_get(response, "output") or [])
            function_calls = _responses_function_calls(output_items)
            content = responses_output_text(response, output_items)
            final_output = content
            if approved_directive is not None:
                if not function_calls:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_plain_final_rejected",
                    )
                if len(function_calls) != 1:
                    await fail_approved_action(
                        approved_directive,
                        reason="approved_action_multiple_calls_rejected",
                    )
            for summary_text in _responses_reasoning_summaries(output_items):
                # 推論サマリーは中間・最終どちらのラウンドでもそのまま配信する。
                await _emit(
                    stream_callback,
                    "thinking",
                    {
                        "text": summary_text,
                        "kind": "summary",
                        "round": round_index,
                    },
                )
            response_id = getattr(response, "id", None)
            if response_id is None and isinstance(response, dict):
                response_id = response.get("id")
            if (
                self.conversation_state_mode == "provider-managed"
                and response_id
            ):
                self.provider_state.previous_response_id = str(response_id)

            if not function_calls:
                if not content.strip():
                    failure = _empty_response_failure(
                        transport="responses",
                        response=response,
                        output_items=output_items,
                    )
                    partial_result = NativeRunResult(
                        final_output="",
                        messages=[
                            *chat_messages,
                            {"role": "assistant", "content": content},
                        ],
                        tool_calls=list(tool_records),
                        usage_records=list(usage_records),
                        context_snapshots=list(context_snapshots),
                        generation_failure=failure,
                    )
                    await _emit(stream_callback, "stream_end", {"content": ""})
                    return partial_result
                if content:
                    content = str(self.privacy_gateway.restore_aliases(content))
                    await _emit(stream_callback, "stream_token", {"content": content})
                await _emit(stream_callback, "stream_end", {"content": content})
                if content:
                    chat_messages.append({"role": "assistant", "content": content})
                return NativeRunResult(
                    final_output=content,
                    messages=list(chat_messages),
                    tool_calls=list(tool_records),
                    usage_records=list(usage_records),
                    context_snapshots=list(context_snapshots),
                )

            # reasoning item を含む前ラウンドの output をそのまま次の input に引き継ぐ。
            # reasoning を落とすと terra 系でエラーや品質劣化の恐れがあるため全量保持する。
            serialized_output_items = _serialize_responses_output_items(
                output_items,
                registry=tool_registry,
            )
            stateless_input_items.extend(serialized_output_items)
            if self.conversation_state_mode == "provider-managed":
                # The provider already owns the previous response.  Only the
                # new function outputs belong in the next request.
                input_items = []
            else:
                input_items.extend(serialized_output_items)
            chat_messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        _responses_call_to_chat_tool_call(
                            fc,
                            registry=tool_registry,
                        )
                        for fc in function_calls
                    ],
                }
            )
            if content:
                # function_call を伴うラウンドの通常テキストは途中経過として配信する。
                # 最終ラウンドは stream_token 側で配信するためここでは発行しない。
                await _emit(
                    stream_callback,
                    "assistant_text",
                    {"text": content, "round": round_index},
                )

            for function_call_index, function_call in enumerate(function_calls):
                tool_name = _responses_call_name(function_call)
                observable_name = observable_tool_name(tool_registry, tool_name)
                provider_call_id = _responses_call_id(function_call)
                call_id = (
                    approved_directive.call_id
                    if approved_directive is not None
                    else provider_call_id
                )
                args, parse_error = _responses_call_arguments(function_call)
                if not parse_error and approved_directive is not None:
                    args = self.privacy_gateway.restore_tool_arguments(
                        args,
                        tool_name=tool_name,
                    )
                if approved_directive is not None:
                    if parse_error:
                        await fail_approved_action(
                            approved_directive,
                            reason="approved_action_arguments_invalid",
                            detail=parse_error,
                        )
                    try:
                        args = bind_approved_action_call(
                            approved_directive,
                            tool_name=tool_name,
                            proposed_arguments=args,
                        )
                    except PlanningInteractionTerminated as exc:
                        await fail_approved_action(
                            approved_directive,
                            reason=exc.reason,
                        )
                event_args = (
                    {}
                    if parse_error
                    else _observable_tool_arguments(
                        tool_registry, tool_name, args
                    )
                )
                await _emit(
                    stream_callback,
                    "tool_start",
                    {
                        "tool": observable_name,
                        "tool_args": event_args,
                        "operation_id": call_id,
                        "tool_call_id": call_id,
                        "message": f"{observable_name} を実行しています",
                    },
                )

                execution_success = False
                execution_error = parse_error or ""
                audit_args = dict(event_args)
                if parse_error:
                    result_text = f"Error: invalid JSON arguments: {parse_error}"
                elif _simple_task_unexpected_mutation_execution_blocked(
                    plain_user_input,
                    tool_name,
                    tool_records,
                ):
                    hard_stop = True
                    result_text = json.dumps(
                        {
                            "success": False,
                            "error_code": "unexpected_task_mutation",
                            "error": (
                                "simple task creation permits only duplicate search "
                                "followed by create_task"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    execution_error = "unexpected_task_mutation"
                elif (
                    tool_name == "create_task"
                    and not _simple_task_create_execution_allowed(
                        plain_user_input,
                        tool_records,
                    )
                ):
                    hard_stop = True
                    result_text = json.dumps(
                        {
                            "success": False,
                            "error_code": "duplicate_search_required",
                            "error": (
                                "create_task requires a successful, unambiguous "
                                "empty search_task_candidates result first"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    execution_error = "duplicate_search_required"
                else:
                    if approved_directive is None:
                        args = self.privacy_gateway.restore_tool_arguments(
                            args,
                            tool_name=tool_name,
                        )
                    tool_result = await tool_router.execute_async(
                        UnifiedToolCall(
                            tool=tool_name,
                            arguments=args,
                            call_id=call_id,
                        )
                    )
                    result_text = tool_result.model_output
                    execution_success = tool_result.success
                    execution_error = tool_result.error or ""
                    audit_args = _observable_tool_arguments(
                        tool_registry,
                        tool_name,
                        tool_result.call.arguments,
                    )

                model_payload = model_tool_result_payload(
                    tool_name=observable_name,
                    output=result_text,
                    user_input=plain_user_input,
                    max_chars=self.max_tool_result_chars,
                    config=self.config,
                    legacy_clip=_clip_text,
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool=observable_name,
                        arguments=dict(audit_args),
                        result=result_text,
                    )
                )
                if tool_name == "create_task" and _simple_task_request_is_explicit(
                    plain_user_input
                ):
                    if (
                        simple_task_mutation_completion_state(
                            plain_user_input,
                            tool_records,
                        )
                        is SimpleTaskMutationCompletionState.COMPLETE
                    ):
                        authoritative_task_complete = True
                    else:
                        # See the chat-completions path: never retry a create
                        # whose durable receipt is not proven.
                        hard_stop = True
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": provider_call_id,
                        "output": model_payload.text,
                    }
                )
                stateless_input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": provider_call_id,
                        "output": model_payload.text,
                    }
                )
                chat_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": provider_call_id,
                        "content": model_payload.text,
                    }
                )
                await _emit(
                    stream_callback,
                    "tool_end",
                    {
                        "tool": observable_name,
                        "tool_args": audit_args,
                        "operation_id": call_id,
                        "tool_call_id": call_id,
                        "tool_result": {
                            "tool": observable_name,
                            "arguments": audit_args,
                            "output": result_text,
                            "error": None if execution_success else (execution_error or result_text),
                            "mutation_confirmed": bool(
                                approved_directive is not None and execution_success
                            ),
                            "tool_call_id": call_id,
                        },
                        "message": "ツール実行が完了しました",
                    },
                )

                if approved_directive is not None:
                    await persist_and_accept_approved_action_result(
                        approved_directive,
                        arguments=audit_args,
                        result=result_text,
                        success=execution_success,
                    )
                elif (
                    tool_name == "submit_plan_for_approval"
                    and (state := get_current_planning_run_state()) is not None
                    and state.phase == PlanningRunPhase.EXECUTING
                ):
                    for skipped in function_calls[function_call_index + 1 :]:
                        skipped_output = {
                            "type": "function_call_output",
                            "call_id": _responses_call_id(skipped),
                            "output": (
                                "Not executed: approved actions start on the next "
                                "server-controlled round."
                            ),
                        }
                        input_items.append(skipped_output)
                        stateless_input_items.append(dict(skipped_output))
                        chat_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _responses_call_id(skipped),
                                "content": skipped_output["output"],
                            }
                        )
                    break

                if hard_stop or authoritative_task_complete:
                    for skipped in function_calls[function_call_index + 1 :]:
                        skipped_output = {
                            "type": "function_call_output",
                            "call_id": _responses_call_id(skipped),
                            "output": (
                                "Not executed: the deterministic task mutation "
                                "was already terminal (or duplicate search failed)."
                            ),
                        }
                        input_items.append(skipped_output)
                        stateless_input_items.append(dict(skipped_output))
                        chat_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _responses_call_id(skipped),
                                "content": skipped_output["output"],
                            }
                        )
                    break

            if round_index == 0 and current_tool_choice == "required":
                current_tool_choice = "auto"
            if hard_stop:
                break

        fallback = str(
            self.privacy_gateway.restore_aliases(
                final_output or "ツール実行後の最終応答を生成できませんでした。"
            )
        )
        await _emit(stream_callback, "stream_token", {"content": fallback})
        await _emit(stream_callback, "stream_end", {"content": fallback})
        logger.warning("Native agent turn exceeded max tool rounds: %s", agent.name)
        return NativeRunResult(
            final_output=fallback,
            messages=list(chat_messages),
            tool_calls=list(tool_records),
            usage_records=list(usage_records),
            context_snapshots=list(context_snapshots),
            tool_rounds_exhausted=True,
        )

    def _build_completion_kwargs(
        self,
        *,
        agent: AgentDefinition,
        messages: list[dict[str, Any]],
        tools_payload: list[dict[str, Any]],
        tool_choice: Optional[str],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": agent.model,
            # The loop appends the final assistant item after the transport
            # call.  Copy the outer list so fake/real SDK request payloads are
            # not mutated after submission.
            "messages": [dict(message) for message in messages],
        }
        if tools_payload:
            kwargs["tools"] = tools_payload
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if self.config and hasattr(self.config, "get"):
            max_output_tokens = self.config.get(
                "runtime.target_max_output_tokens", None
            )
            if max_output_tokens:
                token_key = (
                    "max_completion_tokens"
                    if self.provider_label == "kimi" and agent.model == "kimi-k3"
                    else "max_tokens"
                )
                kwargs[token_key] = max(1, int(max_output_tokens))
            extra_body = self.config.get("free_team.request_extra_body", None)
            if isinstance(extra_body, dict) and extra_body:
                kwargs["extra_body"] = dict(extra_body)
        if self.provider_label == "openrouter":
            # OpenRouter は usage.include を明示しない限り usage.cost を返さない。
            # 既存の extra_body 指定は保持し、usage キーだけをマージする。
            merged_extra_body = dict(kwargs.get("extra_body") or {})
            usage_option = merged_extra_body.get("usage")
            usage_option = dict(usage_option) if isinstance(usage_option, dict) else {}
            usage_option.setdefault("include", True)
            merged_extra_body["usage"] = usage_option
            kwargs["extra_body"] = merge_provider_options_into_extra_body(
                merged_extra_body,
                self.config,
                agent.model,
            )
        if self.provider_label == "deepseek":
            reasoning = getattr(getattr(agent, "model_settings", None), "reasoning", None)
            effort = str(getattr(reasoning, "effort", "") or "").strip().lower()
            agent_team_effort = str(
                self.config.get("runtime.agent_team_effective_effort", "")
                if self.config and hasattr(self.config, "get")
                else ""
            ).strip().lower()
            agent_team_route = bool(
                self.config
                and hasattr(self.config, "get")
                and str(self.config.get("runtime.agent_team_effort_policy", "") or "").strip()
            )
            if agent_team_route:
                # The route resolver has already checked this value against
                # the actual session Main model.  Empty means an unsupported
                # explicit request was intentionally dropped; do not fall
                # back to DeepSeek's global default.
                effort = agent_team_effort
            elif not effort and self.config and hasattr(self.config, "get"):
                effort = str(
                    self.config.get("deepseek.reasoning_effort", "high") or "high"
                ).strip().lower()
            if effort and effort not in {"none", "high", "max"}:
                effort = "" if agent_team_route else "high"
            if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                configured_max = (
                    self.config.get("runtime.target_max_output_tokens", 2048)
                    if self.config and hasattr(self.config, "get")
                    else 2048
                )
                kwargs["max_tokens"] = max(1, int(configured_max or 2048))
            if agent_team_route and not effort:
                # A caller/model preset may already have placed a provider
                # effort in the request body.  An unsupported explicit Team
                # value is fail-closed, so remove that stale value rather
                # than allowing it to reach DeepSeek or resurrecting a mode
                # from ``free_team.request_extra_body``.
                kwargs.pop("reasoning_effort", None)
                stale_extra_body = dict(kwargs.get("extra_body") or {})
                stale_extra_body.pop("reasoning_effort", None)
                stale_extra_body.pop("thinking", None)
                if stale_extra_body:
                    kwargs["extra_body"] = stale_extra_body
                else:
                    kwargs.pop("extra_body", None)
            if effort:
                merged_extra_body = dict(kwargs.get("extra_body") or {})
                merged_extra_body["thinking"] = {
                    "type": "disabled" if effort == "none" else "enabled"
                }
                kwargs["extra_body"] = merged_extra_body
                if effort == "none":
                    kwargs.pop("reasoning_effort", None)
                else:
                    kwargs["reasoning_effort"] = effort
                    if tools_payload:
                        # DeepSeek's thinking mode does not accept tool_choice.
                        kwargs.pop("tool_choice", None)
                    for key in (
                        "temperature",
                        "top_p",
                        "n",
                        "presence_penalty",
                        "frequency_penalty",
                    ):
                        kwargs.pop(key, None)
        if self.provider_label == "deepinfra":
            reasoning = getattr(getattr(agent, "model_settings", None), "reasoning", None)
            effort = str(getattr(reasoning, "effort", "") or "").strip().lower()
            agent_team_effort = str(
                self.config.get("runtime.agent_team_effective_effort", "")
                if self.config and hasattr(self.config, "get")
                else ""
            ).strip().lower()
            agent_team_route = bool(
                self.config
                and hasattr(self.config, "get")
                and str(self.config.get("runtime.agent_team_effort_policy", "") or "").strip()
            )
            if agent_team_route:
                effort = agent_team_effort
            elif not effort and self.config and hasattr(self.config, "get"):
                effort = str(
                    self.config.get("deepinfra.reasoning_effort", "high") or "high"
                ).strip().lower()
            if effort and effort not in {"none", "low", "medium", "high"}:
                effort = "" if agent_team_route else "high"
            if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                configured_max = (
                    self.config.get("runtime.target_max_output_tokens", 2048)
                    if self.config and hasattr(self.config, "get")
                    else 2048
                )
                kwargs["max_tokens"] = max(1, int(configured_max or 2048))
            if agent_team_route and not effort:
                # Drop only the provider-specific effort key; unrelated
                # request extras (for example prompt-cache metadata) remain
                # intact for a valid API request.
                stale_extra_body = dict(kwargs.get("extra_body") or {})
                stale_extra_body.pop("reasoning_effort", None)
                if stale_extra_body:
                    kwargs["extra_body"] = stale_extra_body
                else:
                    kwargs.pop("extra_body", None)
            if effort:
                merged_extra_body = dict(kwargs.get("extra_body") or {})
                merged_extra_body["reasoning_effort"] = effort
                if self.prompt_cache_key:
                    merged_extra_body["prompt_cache_key"] = self.prompt_cache_key
                kwargs["extra_body"] = merged_extra_body
                for key in (
                    "temperature",
                    "top_p",
                    "n",
                    "presence_penalty",
                    "frequency_penalty",
                ):
                    kwargs.pop(key, None)
        if self.provider_label == "kimi" and agent.model == "kimi-k3":
            kimi_effort = "max"
            team_effort_policy = str(
                self.config.get("runtime.agent_team_effort_policy", "")
                if self.config and hasattr(self.config, "get")
                else ""
            ).strip().lower()
            if team_effort_policy in {"same", "lower", "explicit", "default"}:
                requested = str(
                    self.config.get("runtime.agent_team_effective_effort", "")
                    if self.config and hasattr(self.config, "get")
                    else ""
                ).strip()
                from ..services.llm_model_catalog import reasoning_effort_options_for_model

                options = reasoning_effort_options_for_model("kimi", agent.model)
                kimi_effort = requested if requested in options else ""
            if kimi_effort:
                kwargs["reasoning_effort"] = kimi_effort
            else:
                kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            for key in (
                "temperature",
                "top_p",
                "n",
                "presence_penalty",
                "frequency_penalty",
                "thinking",
            ):
                kwargs.pop(key, None)
        return kwargs


def _safe_transport_origin(value: Any) -> str | None:
    """Return scheme://host[:port] only; discard path/query/userinfo."""

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
    return (
        f"{scheme}://{host}"
        f"{f':{port}' if port is not None else ''}"
    )


def _environment_proxy_diagnostic(base_url: Any) -> dict[str, Any]:
    """Observe the environment proxy inputs used by the default SDK client.

    This is diagnostic-only. It does not inject an HTTP client, change the
    endpoint, bypass a proxy, or retry a refused provider request.
    """

    origin = _safe_transport_origin(base_url)
    result: dict[str, Any] = {
        "base_url_origin": origin,
        "proxy_mode": "sdk_environment",
        "proxy_configured": False,
        "proxy_bypassed": False,
    }
    if not origin:
        return result

    try:
        target = urlsplit(origin)
        proxies = {
            str(key or "").strip().casefold(): str(value or "").strip()
            for key, value in getproxies().items()
            if str(key or "").strip() and str(value or "").strip()
        }
    except Exception:
        return result

    scheme = str(target.scheme or "").casefold()
    proxy_value = proxies.get(scheme)
    proxy_source = f"{scheme}_proxy" if proxy_value else ""
    if not proxy_value:
        proxy_value = proxies.get("all")
        proxy_source = "all_proxy" if proxy_value else ""
    if not proxy_value:
        return result

    result["proxy_configured"] = True
    result["proxy_source"] = proxy_source
    # ``proxy_bypass`` expects the hostname (not an authority with a port);
    # NO_PROXY entries are conventionally host/domain patterns.
    target_host = str(target.hostname or "")
    try:
        bypassed = bool(proxy_bypass(target_host))
    except Exception:
        bypassed = False
    result["proxy_bypassed"] = bypassed
    if not bypassed:
        proxy_origin = _safe_transport_origin(proxy_value)
        if proxy_origin:
            result["proxy_origin"] = proxy_origin
    return result


def _sanitize_transport_diagnostic(
    value: Any,
    *,
    fallback_base_url: Any = None,
) -> dict[str, Any]:
    """Reduce stored/custom transport diagnostics to safe bounded fields."""

    raw = dict(value) if isinstance(value, dict) else {}
    result: dict[str, Any] = {
        "proxy_configured": bool(raw.get("proxy_configured", False)),
        "proxy_bypassed": bool(raw.get("proxy_bypassed", False)),
    }

    base_origin = (
        _safe_transport_origin(raw.get("base_url_origin"))
        or _safe_transport_origin(fallback_base_url)
    )
    if base_origin:
        result["base_url_origin"] = base_origin

    proxy_mode = str(raw.get("proxy_mode") or "").strip().casefold()
    if proxy_mode == "sdk_environment":
        result["proxy_mode"] = proxy_mode

    proxy_source = str(raw.get("proxy_source") or "").strip().casefold()
    if proxy_source in {"http_proxy", "https_proxy", "all_proxy"}:
        result["proxy_source"] = proxy_source

    proxy_origin = _safe_transport_origin(raw.get("proxy_origin"))
    if proxy_origin and not result["proxy_bypassed"]:
        result["proxy_origin"] = proxy_origin
        result["proxy_configured"] = True

    return result


def openai_transport_diagnostic(client: Any) -> dict[str, Any]:
    """Return the secret-free transport observation for a constructed client."""

    stored = getattr(client, "_aoitalk_transport_diagnostic", None)
    if isinstance(stored, dict):
        raw = stored
    else:
        raw = _environment_proxy_diagnostic(
            getattr(client, "base_url", None)
        )
    return _sanitize_transport_diagnostic(
        raw,
        fallback_base_url=getattr(client, "base_url", None),
    )


def create_async_openai_client(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    default_headers: Optional[dict[str, str]] = None,
) -> AsyncOpenAI:
    # The privacy gateway owns retry/review semantics for every model
    # request.  Disable the SDK's implicit retries so a reviewed payload is
    # never replayed behind the gateway's back.
    kwargs: dict[str, Any] = {
        "api_key": api_key or os.getenv("OPENAI_API_KEY"),
        "max_retries": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if default_headers:
        kwargs["default_headers"] = default_headers
    client = AsyncOpenAI(**kwargs)
    diagnostic = _sanitize_transport_diagnostic(
        _environment_proxy_diagnostic(
            getattr(client, "base_url", None)
        ),
        fallback_base_url=getattr(client, "base_url", None),
    )
    try:
        setattr(
            client,
            "_aoitalk_transport_diagnostic",
            diagnostic,
        )
    except Exception:
        pass
    return client


async def run_native_agent_once(
    agent: AgentDefinition,
    prompt: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    default_headers: Optional[dict[str, str]] = None,
    provider_label: str = "openai",
    config: Any | None = None,
    privacy_gateway: OutboundPrivacyGateway | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    cloud_advisor_origin: Any | None = None,
    cloud_advisor_assessment: Any | None = None,
) -> NativeRunResult:
    runner = AgentTurnRunner(
        client=create_async_openai_client(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
        ),
        provider_label=provider_label,
        config=config,
        privacy_gateway=privacy_gateway,
        session_id=session_id,
        user_id=user_id,
    )
    run_kwargs: dict[str, Any] = {}
    # Do not pass new keyword arguments for the historical/default call.  A
    # few integrations replace ``AgentTurnRunner`` with a tiny two-argument
    # test/compatibility runner; keeping the old call shape preserves them.
    if cloud_advisor_origin is not None:
        run_kwargs["cloud_advisor_origin"] = cloud_advisor_origin
    if cloud_advisor_assessment is not None:
        run_kwargs["cloud_advisor_assessment"] = cloud_advisor_assessment
    return await runner.run(agent, prompt, **run_kwargs)


def _tool_specs(tools: Iterable[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.to_json_schema(),
            },
        }
        for tool in tools
    ]


def _resolve_runtime_tools(
    agent: AgentDefinition,
    tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None,
) -> list[ToolDefinition]:
    """Resolve the native tools for one model round.

    Tool-pack/session resolvers are intentionally best-effort: a resolver
    failure must not make the model turn disappear, and the static agent tool
    list remains the safe fallback for legacy/direct callers.
    """
    # ``AgentTurnRunner`` is also a public low-level entrypoint.  Do not rely
    # solely on TerminalMode/tool-exposure filtering: a direct Help caller can
    # supply a static AgentDefinition containing arbitrary mutation tools.
    # The trusted controller scope is always tool-free, including resolver
    # failure paths where the normal compatibility fallback would reuse those
    # static definitions.
    try:
        from ..services.turn_context import get_turn_context

        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return []
    except Exception:
        pass
    if tools_provider is None:
        return list(agent.tools)
    try:
        return [ensure_tool_definition(tool) for tool in tools_provider(agent)]
    except Exception:  # noqa: BLE001
        logger.warning("dynamic native tool resolution failed; using static tools", exc_info=True)
        return list(agent.tools)


def _responses_tool_specs(tools: Iterable[ToolDefinition]) -> list[dict[str, Any]]:
    """Responses API のフラットな function tool 定義を組み立てる。

    chat.completions の ``{"type":"function","function":{...}}`` ネスト形式と異なり、
    Responses は ``{"type":"function","name":...}`` のフラット形式を要求する。
    """
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.to_json_schema(),
            "strict": False,
        }
        for tool in tools
    ]


def _responses_user_content(user_input: str | list[dict[str, Any]]) -> Any:
    if isinstance(user_input, str):
        return user_input or ""
    parts: list[dict[str, Any]] = []
    for part in user_input or []:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text"}:
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part_type in {"image_url", "input_image"}:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                parts.append({"type": "input_image", "image_url": str(image_url)})
    if not parts:
        return ""
    return parts


def _prompt_messages_or_user(
    value: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(value, list) and any(
        isinstance(item, dict) and item.get("role") for item in value
    ):
        return [dict(item) for item in value if isinstance(item, dict) and item.get("role")]
    return [{"role": "user", "content": _responses_user_content(value)}]


def _prompt_messages_for_isolated_help(
    value: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only the current user message for a direct Help invocation.

    ``AgentTurnRunner`` is public and a few integrations call it directly
    with a pre-built message list.  The normal terminal path passes a single
    Guide-grounded string, but accepting that list verbatim here would let a
    stale conversation/tool result bypass the controller's stateless
    boundary.  Preserve the last user item (including an explicitly attached
    image) and let ``agent.instructions`` remain the sole system prompt.
    """

    messages = _prompt_messages_or_user(value)
    for message in reversed(messages):
        if str(message.get("role") or "") == "user":
            return [dict(message)]
    return [{"role": "user", "content": ""}]


def _responses_input_items(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical chat roles to Responses input items."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or "call_unknown"),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = _responses_user_content(content)
        item = {"role": role, "content": content if content is not None else ""}
        if role == "assistant" and message.get("tool_calls"):
            # Responses accepts function_call items rather than chat's nested
            # tool_calls.  Keep text assistant messages and add calls in order.
            item.pop("content", None)
            items.append(item) if item.get("content") else None
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") if isinstance(call, dict) else "call_unknown"),
                        "name": str((function or {}).get("name") if isinstance(function, dict) else ""),
                        "arguments": str((function or {}).get("arguments") if isinstance(function, dict) else "{}"),
                    }
                )
            continue
        items.append(item)
    return items


def _responses_item_get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _responses_item_type(item: Any) -> str:
    return str(_responses_item_get(item, "type") or "")


def _responses_item_contains_image(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get("type") or "") in {"input_image", "image_url"}:
            return True
        return any(_responses_item_contains_image(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_responses_item_contains_image(item) for item in value)
    return False


def _responses_function_calls(output_items: Iterable[Any]) -> list[Any]:
    return [
        item for item in output_items if _responses_item_type(item) == "function_call"
    ]


def _responses_reasoning_summaries(output_items: Iterable[Any]) -> list[str]:
    """reasoning item の summary テキストを item ごとに 1 本へまとめて返す。"""
    summaries: list[str] = []
    for item in output_items or []:
        if _responses_item_type(item) != "reasoning":
            continue
        summary = _responses_item_get(item, "summary")
        if isinstance(summary, str):
            entries: list[Any] = [summary]
        elif isinstance(summary, (list, tuple)):
            entries = list(summary)
        else:
            entries = []
        parts: list[str] = []
        for entry in entries:
            text = entry if isinstance(entry, str) else _responses_item_get(entry, "text")
            if text is not None and str(text).strip():
                parts.append(str(text))
        if parts:
            summaries.append("\n\n".join(parts))
    return summaries


def _has_reasoning_summary(request_kwargs: dict[str, Any]) -> bool:
    reasoning = request_kwargs.get("reasoning")
    return isinstance(reasoning, dict) and "summary" in reasoning


def _without_reasoning_summary(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    retry_kwargs = dict(request_kwargs)
    reasoning = retry_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        stripped = {key: value for key, value in reasoning.items() if key != "summary"}
        if stripped:
            retry_kwargs["reasoning"] = stripped
        else:
            retry_kwargs.pop("reasoning", None)
    return retry_kwargs


def _bad_request_status(exc: Exception) -> bool:
    """openai.BadRequestError 相当（HTTP 400）かどうか。"""
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if candidate is None:
            continue
        try:
            return int(candidate) == 400
        except (TypeError, ValueError):
            continue
    # status を持たない SDK ラッパー例外向けにクラス名でも判定する。
    return type(exc).__name__ == "BadRequestError"


def _is_reasoning_summary_error(exc: Exception) -> bool:
    """summary パラメータ非対応が原因と読める 400 エラーかどうか。

    単なる ``"summary" in message`` だとタイムアウトやツール出力由来の
    無関係なエラーまで拾って summary を落としてしまうため、
    400 系かつ reasoning.summary を指していると読める場合だけ True にする。
    """
    if not _bad_request_status(exc):
        return False
    message = str(exc).lower()
    if "reasoning.summary" in message:
        return True
    if "summary" not in message:
        return False
    return any(
        hint in message
        for hint in (
            "reasoning",
            "param",
            "unsupported",
            "not supported",
            "unknown",
            "unrecognized",
        )
    )


def _serialize_responses_output_items(
    output_items: Iterable[Any],
    *,
    registry: ToolRegistry | None = None,
) -> list[Any]:
    """次ラウンドの input に引き継ぐため output item を素の dict へ変換する。"""
    serialized: list[Any] = []
    for item in output_items:
        if isinstance(item, dict):
            payload = dict(item)
        else:
            dump = getattr(item, "model_dump", None)
            if callable(dump):
                try:
                    payload = dump(exclude_none=True)
                except Exception:  # noqa: BLE001
                    payload = item
            else:
                payload = item
        if registry is not None and isinstance(payload, dict):
            if str(payload.get("type") or "") == "function_call":
                provider_name = str(payload.get("name") or "")
                raw_arguments = payload.get("arguments", "{}")
                if isinstance(raw_arguments, dict):
                    parsed_arguments = raw_arguments
                else:
                    try:
                        parsed_arguments = json.loads(raw_arguments or "{}")
                    except Exception:
                        parsed_arguments = {}
                payload["name"] = observable_tool_name(registry, provider_name)
                payload["arguments"] = json.dumps(
                    _observable_tool_arguments(
                        registry,
                        provider_name,
                        parsed_arguments if isinstance(parsed_arguments, dict) else {},
                    ),
                    ensure_ascii=False,
                )
        serialized.append(payload)
    return serialized


def responses_output_text(response: Any, output_items: Optional[Iterable[Any]] = None) -> str:
    """Responses レスポンスから最終テキストを取り出す。"""
    text = getattr(response, "output_text", None)
    if isinstance(response, dict):
        text = response.get("output_text")
    if isinstance(text, str) and text:
        return text

    if output_items is None:
        output_items = _responses_item_get(response, "output") or []
    parts: list[str] = []
    for item in output_items or []:
        if _responses_item_type(item) != "message":
            continue
        content = _responses_item_get(item, "content") or []
        for chunk in content:
            chunk_text = _responses_item_get(chunk, "text")
            if chunk_text:
                parts.append(str(chunk_text))
    return "".join(parts)


def _responses_call_name(function_call: Any) -> str:
    return str(_responses_item_get(function_call, "name") or "")


def _responses_call_id(function_call: Any) -> str:
    call_id = _responses_item_get(function_call, "call_id")
    if call_id:
        return str(call_id)
    return str(_responses_item_get(function_call, "id") or f"call_{uuid.uuid4().hex}")


def _responses_call_raw_arguments(function_call: Any) -> str:
    arguments = _responses_item_get(function_call, "arguments")
    if isinstance(arguments, str):
        return arguments
    if arguments is not None:
        return json.dumps(arguments, ensure_ascii=False)
    return "{}"


def _responses_call_arguments(function_call: Any) -> tuple[dict[str, Any], str | None]:
    raw_arguments = _responses_call_raw_arguments(function_call)
    try:
        parsed = json.loads(raw_arguments or "{}")
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object"
    return parsed, None


def _responses_call_to_chat_tool_call(
    function_call: Any,
    *,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """外部消費用の chat 形式 tool_call dict へ変換する。"""
    provider_name = _responses_call_name(function_call)
    raw_arguments = _responses_call_raw_arguments(function_call)
    if registry is not None:
        try:
            parsed_arguments = json.loads(raw_arguments or "{}")
        except Exception:
            parsed_arguments = {}
        tool_name = observable_tool_name(registry, provider_name)
        arguments = json.dumps(
            _observable_tool_arguments(
                registry,
                provider_name,
                parsed_arguments if isinstance(parsed_arguments, dict) else {},
            ),
            ensure_ascii=False,
        )
    else:
        tool_name = provider_name
        arguments = raw_arguments
    return {
        "id": _responses_call_id(function_call),
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


def _normalize_tool_choice(value: Optional[str], *, has_tools: bool) -> Optional[str]:
    if not has_tools:
        return None
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "required", "none"}:
        return normalized
    return "auto"


def _assistant_message_payload(
    message: Any,
    *,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """Preserve provider fields needed to continue a Chat Completions turn."""
    if isinstance(message, dict):
        payload = dict(message)
    else:
        dumped = None
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(exclude_none=True)
            except TypeError:
                dumped = model_dump()
        payload = dict(dumped) if isinstance(dumped, dict) else {}
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        for field_name in (
            "reasoning_content",
            "reasoning",
            "thinking_content",
            "thinking",
        ):
            if field_name not in payload:
                value = getattr(message, field_name, None)
                if value is not None:
                    payload[field_name] = value
    payload["role"] = str(payload.get("role") or getattr(message, "role", "assistant") or "assistant")
    payload["content"] = payload.get("content") or getattr(message, "content", "") or ""
    tool_calls = list(payload.get("tool_calls") or getattr(message, "tool_calls", None) or [])
    if tool_calls:
        payload["tool_calls"] = [
            _serialize_tool_call(call, registry=registry)
            for call in tool_calls
        ]
    return payload


def _serialize_tool_call(
    tool_call: Any,
    *,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    provider_name = _tool_call_name(tool_call)
    raw_arguments = _tool_call_raw_arguments(tool_call)
    if registry is not None:
        try:
            parsed_arguments = json.loads(raw_arguments or "{}")
        except Exception:
            parsed_arguments = {}
        tool_name = observable_tool_name(registry, provider_name)
        arguments = json.dumps(
            _observable_tool_arguments(
                registry,
                provider_name,
                parsed_arguments if isinstance(parsed_arguments, dict) else {},
            ),
            ensure_ascii=False,
        )
    else:
        tool_name = provider_name
        arguments = raw_arguments
    return {
        "id": _tool_call_id(tool_call),
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or f"call_{uuid.uuid4().hex}")
    return str(getattr(tool_call, "id", "") or f"call_{uuid.uuid4().hex}")


def _tool_call_name(tool_call: Any) -> str:
    function = getattr(tool_call, "function", None)
    if function is not None:
        return str(getattr(function, "name", "") or "")
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
    return ""


def _tool_call_raw_arguments(tool_call: Any) -> str:
    function = getattr(tool_call, "function", None)
    if function is not None:
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str):
            return arguments
        if arguments is not None:
            return json.dumps(arguments, ensure_ascii=False)
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                return arguments
            return json.dumps(arguments or {}, ensure_ascii=False)
    return "{}"


def _tool_call_arguments(tool_call: Any) -> tuple[dict[str, Any], str | None]:
    raw_arguments = _tool_call_raw_arguments(tool_call)
    try:
        parsed = json.loads(raw_arguments or "{}")
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object"
    return parsed, None


def _clip_text(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    suffix = "\n... (truncated to fit the model context budget)"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


async def _emit(
    callback: Optional[StreamCallback],
    event: str,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    result = callback(event, payload)
    if inspect.isawaitable(result):
        await result
