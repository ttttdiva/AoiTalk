"""Unified turn runtime primitives shared by provider adapters.

The runtime keeps the provider-specific transport at the edges and owns the
agent loop shape: model output -> tool execution -> tool result input ->
follow-up sampling.  It is intentionally small, but it gives OpenAI-style,
CLI-style, and specialist runners the same execution contract.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..tools.registry import ToolRegistry
from ..services.agent_team_service import (
    ToolFailureCircuitBreaker,
    parse_structured_tool_failure,
    tool_failure_family,
)
from ..services.agent_team_v3 import agent_team_v3_delegation_enabled
from .context_compression import model_tool_result_payload
from .generation_cancellation import GenerationInterrupted
from .turn_stream_events import (
    SyncStreamEmitter,
    emit_assistant_text,
    emit_thinking,
    thinking_text_from_message,
)

logger = logging.getLogger(__name__)

# A CLI provider can stop after producing a tool marker which was not
# executed (for example when the follow-up process fails or the round budget
# is exhausted).  Keep this payload deliberately provider-neutral and free of
# the raw marker so callers that do not have their own failure mapping (such
# as specialist delegation) cannot persist/display an executable-looking
# pseudo tool call as the assistant answer.
CLI_TOOL_LOOP_FAILURE_MESSAGE = (
    "ツール処理を完了できませんでした。"
    "未完了の操作は実行していないため、内容を確認してから再実行してください。"
)
DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS = 32_000
MAX_CLI_TOOL_CONTEXT_MAX_CHARS = 64_000


def _follow_up_tool_choice(value: Any) -> Any:
    """Relax a one-shot required tool choice for the next model turn.

    OpenAI-compatible providers encode a required function either as the
    string ``"required"`` or as a function-selection mapping.  The latter
    must be reset too; otherwise a successful `/search` (or another explicit
    command) can keep forcing the same function on every follow-up round.
    """

    if value == "required":
        return "auto"
    if isinstance(value, Mapping):
        choice_type = str(value.get("type") or "").casefold()
        function = value.get("function")
        if choice_type == "function" and isinstance(function, Mapping):
            if str(function.get("name") or "").strip():
                return "auto"
    return value


def tool_output_indicates_success(output: Any) -> tuple[bool, str]:
    """Interpret structured tool failures instead of treating every return as success."""
    text = str(output).strip()
    payload: Any = output
    if not isinstance(payload, Mapping):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, Mapping) and payload.get("success") is False:
        return False, str(payload.get("error") or "tool reported failure")
    lowered = text.casefold()
    if lowered.startswith(("error:", "tool execution error:", "tool not found")):
        return False, text
    return True, ""


@dataclass(frozen=True)
class UnifiedToolCall:
    """Provider-neutral tool call."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    raw: Any = None


@dataclass(frozen=True)
class UnifiedToolResult:
    """Provider-neutral tool execution result."""

    call: UnifiedToolCall
    output: str
    success: bool
    error: str = ""
    elapsed_ms: float = 0.0

    @property
    def model_output(self) -> str:
        return self.output if self.success else f"Error: {self.error or self.output}"


@dataclass(frozen=True)
class UnifiedTurnResult:
    """Final result of a unified turn loop."""

    final_output: str
    tool_results: list[UnifiedToolResult] = field(default_factory=list)
    rounds: int = 0
    stopped_reason: str = "final"
    messages: list[dict[str, Any]] = field(default_factory=list)
    # ``tool_results`` is the model/display view and may contain synthetic
    # same-batch results.  Audit consumers must use this execution-only view.
    audit_tool_results: list[UnifiedToolResult] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[UnifiedToolCall]:
        return [result.call for result in self.tool_results]


@dataclass
class UnifiedTurnLedger:
    """Mutable tool-result ledger shared by retries of one logical turn."""

    run_id: str | None = None
    results: list[UnifiedToolResult] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    failure: str = ""
    on_result: Callable[[UnifiedToolResult], None] | None = None

    def record(self, result: UnifiedToolResult) -> None:
        self.results.append(result)
        if self.on_result is not None:
            self.on_result(result)

    def record_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Accumulate confirmed usage from every completion in this turn."""

        if not isinstance(usage, Mapping):
            return
        normalized: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            value = usage.get(key)
            if value is None:
                continue
            try:
                normalized[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        if not normalized:
            return
        normalized.setdefault(
            "total_tokens",
            normalized.get("input_tokens", 0) + normalized.get("output_tokens", 0),
        )
        for key, value in normalized.items():
            self.usage[key] = self.usage.get(key, 0) + value


_ACTIVE_TURN_LEDGER: ContextVar[UnifiedTurnLedger | None] = ContextVar(
    "unified_turn_ledger",
    default=None,
)


@contextmanager
def activate_unified_turn_ledger(
    ledger: UnifiedTurnLedger,
) -> Iterator[UnifiedTurnLedger]:
    """Expose an explicit ledger to wrappers that cannot forward new kwargs."""

    token = _ACTIVE_TURN_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_TURN_LEDGER.reset(token)


class RegistryToolRouter:
    """Execute unified calls through the existing ToolRegistry."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        log_prefix: str = "UnifiedTurn",
        config: Any | None = None,
        user_input: str | None = None,
        enforce_tool_policy: bool = True,
        failure_breaker: ToolFailureCircuitBreaker | None = None,
    ):
        self.registry = registry
        self.log_prefix = log_prefix
        self.config = config
        self.user_input = user_input
        self.enforce_tool_policy = enforce_tool_policy
        # The breaker is request/turn scoped.  Only schema-v3 Agent Team delegation
        # turns opt in by default; ordinary chat keeps its historical retry
        # behavior unless a caller explicitly supplies a breaker.
        if failure_breaker is not None:
            self.failure_breaker = failure_breaker
        elif (
            config is not None
            and agent_team_v3_delegation_enabled(config)
        ):
            self.failure_breaker = ToolFailureCircuitBreaker(
                max_same_failure=2,
                failed_tool_budget=8,
            )
        else:
            self.failure_breaker = None

    def _breaker_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        breaker = self.failure_breaker
        if breaker is None:
            return None
        family = tool_failure_family(call.tool)
        if not breaker.is_open(family):
            return None
        payload = {
            "success": False,
            "error_code": "circuit_open",
            "retryable": False,
            "error": f"Tool failure circuit is open for {family}; retry suppressed",
        }
        logger.warning(
            "[%s] Tool failure circuit open; suppressing %s (%s)",
            self.log_prefix,
            call.tool,
            family,
        )
        return UnifiedToolResult(
            call=call,
            output=json.dumps(payload, ensure_ascii=False),
            success=False,
            error=payload["error"],
        )

    def _apply_failure_breaker(
        self,
        call: UnifiedToolCall,
        result: UnifiedToolResult,
    ) -> UnifiedToolResult:
        breaker = self.failure_breaker
        if breaker is None or result.success:
            return result
        parsed = parse_structured_tool_failure(result.output)
        if parsed is None:
            lowered = str(result.output or result.error or "").strip().lower()
            legacy_code = (
                "not_found"
                if lowered.startswith(("tool not found", "not found:"))
                else "ambiguous_target"
                if "ambiguous" in lowered
                else "validation"
                if "validation" in lowered or "invalid" in lowered
                else "tool_error"
            )
            failure = {
                "success": False,
                "error_code": legacy_code,
                "retryable": False,
                "error": result.error or result.output,
            }
        else:
            failure = parsed
        decision = breaker.check(
            tool_failure_family(call.tool),
            failure,
        )
        if decision.allowed:
            return result
        signature = decision.signature.key if decision.signature else tool_failure_family(call.tool)
        payload = {
            "success": False,
            "error_code": "circuit_open",
            "retryable": False,
            "error": (
                "内部障害の同一原因を繰り返したため、このturnの再試行を停止しました"
                f" ({signature})"
            ),
            "retry_suppressed": True,
        }
        logger.warning(
            "[%s] Tool failure circuit opened for %s: %s",
            self.log_prefix,
            call.tool,
            signature,
        )
        return UnifiedToolResult(
            call=call,
            output=json.dumps(payload, ensure_ascii=False),
            success=False,
            error=payload["error"],
            elapsed_ms=result.elapsed_ms,
        )

    def _policy_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        if not self.enforce_tool_policy:
            return None
        if self.config is None and call.tool == "search_past_chats":
            return None
        try:
            from .tool_policy import (
                check_tool_call_allowed,
                format_blocked_tool_result,
                get_current_user_input,
            )

            decision = check_tool_call_allowed(
                call.tool,
                user_input=self.user_input or get_current_user_input(),
                tool_args=dict(call.arguments or {}),
                config=self.config,
            )
        except Exception:
            logger.error("[%s] Tool policy check failed", self.log_prefix, exc_info=True)
            return UnifiedToolResult(
                call=call,
                output=(
                    "Tool execution was blocked because the deterministic "
                    "tool policy could not be evaluated."
                ),
                success=False,
                error="tool policy unavailable",
            )

        if decision.allowed:
            return None

        output = format_blocked_tool_result(call.tool, decision)
        logger.warning("[%s] Tool policy blocked %s: %s", self.log_prefix, call.tool, decision.reason)
        return UnifiedToolResult(
            call=call,
            output=output,
            success=False,
            error="",
        )

    def _approval_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        registry_get = getattr(self.registry, "get", None)
        if not callable(registry_get):
            return None
        tool_def = registry_get(call.tool)
        if tool_def is None or not getattr(tool_def, "requires_approval", False):
            return None
        try:
            from ..tools.external_llm_permission import check_permission_sync

            approved = check_permission_sync(
                call.tool,
                dict(call.arguments or {}),
                f"Run tool `{call.tool}`",
            )
        except Exception:
            logger.exception("[%s] Tool approval check failed: %s", self.log_prefix, call.tool)
            approved = False
        if approved:
            return None
        return UnifiedToolResult(
            call=call,
            output=f"Tool permission denied: `{call.tool}` was not approved.",
            success=False,
            error="",
        )

    async def _approval_block_result_async(
        self,
        call: UnifiedToolCall,
    ) -> UnifiedToolResult | None:
        registry_get = getattr(self.registry, "get", None)
        if not callable(registry_get):
            return None
        tool_def = registry_get(call.tool)
        if tool_def is None or not getattr(tool_def, "requires_approval", False):
            return None
        try:
            from ..tools.external_llm_permission import check_permission

            approved = await check_permission(
                call.tool,
                dict(call.arguments or {}),
                f"Run tool `{call.tool}`",
            )
        except Exception:
            logger.exception("[%s] Tool approval check failed: %s", self.log_prefix, call.tool)
            approved = False
        if approved:
            return None
        return UnifiedToolResult(
            call=call,
            output=f"Tool permission denied: `{call.tool}` was not approved.",
            success=False,
            error="",
        )

    def execute(self, call: UnifiedToolCall) -> UnifiedToolResult:
        blocked = self._breaker_block_result(call)
        if blocked is not None:
            return blocked
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return blocked
        blocked = self._approval_block_result(call)
        if blocked is not None:
            return blocked

        started = time.perf_counter()
        try:
            from ..services.turn_context import (
                reset_turn_context,
                set_turn_tool_call_id,
            )

            tool_context_token = set_turn_tool_call_id(call.call_id)
            try:
                result = self.registry.execute(call.tool, **dict(call.arguments or {}))
            finally:
                reset_turn_context(tool_context_token)
            output = str(result)
            success, error = tool_output_indicates_success(result)
        except GenerationInterrupted:
            # Steering is a control-flow boundary, not a tool failure.  Let
            # the active response handler regenerate the turn with the delta
            # instead of serializing the exception as a model-visible result.
            raise
        except Exception as exc:
            output = str(exc)
            success = False
            error = str(exc)
            logger.error(
                "[%s] Tool execution failed: %s - %s",
                self.log_prefix,
                call.tool,
                exc,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "[%s] Tool %s -> %s",
            self.log_prefix,
            call.tool,
            (output if success else f"Error: {output}")[:160],
        )
        result = UnifiedToolResult(
            call=call,
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
        )
        return self._apply_failure_breaker(call, result)

    async def execute_async(self, call: UnifiedToolCall) -> UnifiedToolResult:
        blocked = self._breaker_block_result(call)
        if blocked is not None:
            return blocked
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return blocked
        blocked = await self._approval_block_result_async(call)
        if blocked is not None:
            return blocked

        started = time.perf_counter()
        try:
            from ..services.turn_context import (
                reset_turn_context,
                set_turn_tool_call_id,
            )

            tool_context_token = set_turn_tool_call_id(call.call_id)
            try:
                result = await self.registry.execute_async(
                    call.tool,
                    **dict(call.arguments or {}),
                )
            finally:
                reset_turn_context(tool_context_token)
            output = str(result)
            success, error = tool_output_indicates_success(result)
        except GenerationInterrupted:
            # Steering is a control-flow boundary, not a tool failure.  Let
            # the active response handler regenerate the turn with the delta.
            raise
        except Exception as exc:
            output = str(exc)
            success = False
            error = str(exc)
            logger.error(
                "[%s] Tool execution failed: %s - %s",
                self.log_prefix,
                call.tool,
                exc,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "[%s] Tool %s -> %s",
            self.log_prefix,
            call.tool,
            (output if success else f"Error: {output}")[:160],
        )
        result = UnifiedToolResult(
            call=call,
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
        )
        return self._apply_failure_breaker(call, result)

    def execute_many(self, calls: Sequence[UnifiedToolCall]) -> list[UnifiedToolResult]:
        # Keep execution ordered for compatibility.  ToolDefinition already
        # carries supports_parallel; a later pass can safely batch independent
        # calls here without touching provider adapters.
        return [self.execute(call) for call in calls]


def clip_text(text: str, max_chars: int | None) -> str:
    if max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated]"


def openai_message_content(
    message: Any,
    extractor: Callable[[Any], str] | None = None,
) -> str:
    if extractor is not None:
        return str(extractor(message) or "")
    return str(getattr(message, "content", None) or "")


def openai_tool_call_function(tool_call: Any) -> tuple[str, dict[str, Any]]:
    function = getattr(tool_call, "function", None)
    if function is None and isinstance(tool_call, dict):
        function = tool_call.get("function")

    if isinstance(function, dict):
        name = str(function.get("name") or "")
        raw_args = function.get("arguments", "{}")
    else:
        name = str(getattr(function, "name", "") or "")
        raw_args = getattr(function, "arguments", "{}")

    if isinstance(raw_args, dict):
        return name, dict(raw_args)

    try:
        parsed = json.loads(raw_args or "{}")
    except Exception:
        parsed = {}
    return name, parsed if isinstance(parsed, dict) else {}


def openai_tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def serialize_openai_tool_call(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        serialized = dict(tool_call)
        function = serialized.get("function")
        if isinstance(function, dict):
            normalized_function = dict(function)
            arguments = normalized_function.get("arguments", "{}")
            if isinstance(arguments, dict):
                normalized_function["arguments"] = json.dumps(
                    arguments,
                    ensure_ascii=False,
                )
            serialized["function"] = normalized_function
        return serialized

    function = getattr(tool_call, "function", None)
    raw_arguments = getattr(function, "arguments", "{}")
    if isinstance(raw_arguments, dict):
        raw_arguments = json.dumps(raw_arguments, ensure_ascii=False)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": raw_arguments,
        },
    }


def serialize_openai_assistant_message(message: Any) -> dict[str, Any]:
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
    calls = list(payload.get("tool_calls") or getattr(message, "tool_calls", None) or [])
    if calls:
        payload["tool_calls"] = [serialize_openai_tool_call(call) for call in calls]
    return payload


def openai_tool_calls_from_message(message: Any) -> list[UnifiedToolCall]:
    calls = []
    for raw_call in getattr(message, "tool_calls", None) or []:
        name, arguments = openai_tool_call_function(raw_call)
        calls.append(
            UnifiedToolCall(
                tool=name,
                arguments=arguments,
                call_id=openai_tool_call_id(raw_call),
                raw=raw_call,
            )
        )
    return calls


def cli_tool_calls_from_parsed(tool_calls: Sequence[dict[str, Any]]) -> list[UnifiedToolCall]:
    calls: list[UnifiedToolCall] = []
    for index, tool_call in enumerate(tool_calls):
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        calls.append(
            UnifiedToolCall(
                tool=name,
                arguments=dict(args or {}),
                call_id=str(tool_call.get("id") or f"cli-call-{index + 1}"),
                raw=tool_call,
            )
        )
    return calls


def _cli_config_get(config: Any | None, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _cli_tool_context_max_chars(config: Any | None) -> int:
    configured = _cli_config_get(
        config,
        "llm_cli.tool_result_context_max_chars",
        None,
    )
    try:
        value = (
            int(configured)
            if configured is not None
            else DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS
        )
    except (TypeError, ValueError):
        value = DEFAULT_CLI_TOOL_CONTEXT_MAX_CHARS
    # Neither setting may disable the aggregate safety cap.  Keep enough room
    # for at least one compact result plus the completion-control text.
    return max(4_000, min(value, MAX_CLI_TOOL_CONTEXT_MAX_CHARS))


def format_cli_tool_results(
    results: Sequence[UnifiedToolResult],
    *,
    max_chars: int | None = None,
    config: Any | None = None,
    user_input: str = "",
) -> str:
    lines = ["Tool results:"]
    for result in results:
        source = result.output if result.success else result.error or result.output
        payload = model_tool_result_payload(
            tool_name=result.call.tool,
            output=source,
            user_input=user_input,
            max_chars=max_chars,
            config=config,
            legacy_clip=clip_text,
        )
        if result.success:
            lines.append(f"  [{result.call.tool}] {payload.text}")
        else:
            lines.append(f"  [{result.call.tool}] Error: {payload.text}")
    return "\n".join(lines)


def format_cli_tool_history(
    results: Sequence[UnifiedToolResult],
    *,
    original_input: str,
    max_chars: int | None = None,
    max_total_chars: int | None = None,
) -> str:
    """Summarize same-turn CLI tool history for follow-up completion control."""

    results_list = list(results)
    if max_total_chars is not None and max_total_chars > 0:
        unbounded = format_cli_tool_history(
            results_list,
            original_input=original_input,
            max_chars=max_chars,
        )
        if len(unbounded) > max_total_chars:
            # Keep the newest successful/failed operations first.  The
            # current round is supplied separately, so older history is the
            # safest context to discard when the aggregate budget is tight.
            selected = ""
            for count in range(1, len(results_list) + 1):
                candidate = format_cli_tool_history(
                    results_list[-count:],
                    original_input=original_input,
                    max_chars=max_chars,
                )
                if len(candidate) <= max_total_chars:
                    selected = candidate
                    break
            if selected:
                return selected
            # Even one result may exceed a very small budget.  Clip the
            # newest result rather than the oldest prefix of the full history
            # so the follow-up retains the most actionable context.
            newest = format_cli_tool_history(
                results_list[-1:],
                original_input=original_input,
                max_chars=max_chars,
            )
            return newest[:max_total_chars]
        return unbounded

    lines = [
        "Same-turn tool history:",
        f"  Original objective: {original_input}",
    ]
    if not results_list:
        lines.extend(
            [
                "  Already executed tools: none",
                "  Successful tool results: none",
                "  Failed tool results: none",
                "  Remaining original objective: decide whether a tool is required.",
            ]
        )
        return "\n".join(lines)

    executed = [
        f"{index}. {result.call.tool}({json.dumps(result.call.arguments, ensure_ascii=False, sort_keys=True)})"
        for index, result in enumerate(results_list, start=1)
    ]
    successful = []
    failed = []
    for index, result in enumerate(results_list, start=1):
        output = clip_text(result.output, max_chars)
        error = clip_text(result.error or result.output, max_chars)
        entry = f"{index}. [{result.call.tool}] {output}"
        if result.success:
            successful.append(entry)
        else:
            failed.append(f"{index}. [{result.call.tool}] Error: {error}")

    lines.append("  Already executed tools:")
    lines.extend(f"    {entry}" for entry in executed)
    lines.append("  Successful tool results:")
    lines.extend(f"    {entry}" for entry in successful or ["none"])
    lines.append("  Failed tool results:")
    lines.extend(f"    {entry}" for entry in failed or ["none"])
    lines.append(
        "  Remaining original objective: first decide whether the original user request is already satisfied by the successful results above."
    )
    return "\n".join(lines)


def format_cli_follow_up_tool_context(
    current_results: Sequence[UnifiedToolResult],
    all_results: Sequence[UnifiedToolResult],
    *,
    original_input: str,
    max_chars: int | None = None,
    config: Any | None = None,
    user_input: str = "",
) -> str:
    context_limit = _cli_tool_context_max_chars(config)
    current_text = format_cli_tool_results(
        current_results,
        max_chars=max_chars,
        config=config,
        user_input=user_input or original_input,
    )
    completion_text = CLI_TOOL_COMPLETION_CONTROL_PROMPT
    separator_chars = 4  # two ``\n\n`` separators
    current_budget = max(1_000, context_limit - len(completion_text) - separator_chars)
    if len(current_text) > current_budget:
        current_text = clip_text(current_text, current_budget)
    history_budget = max(
        1_000,
        context_limit - len(completion_text) - len(current_text) - separator_chars,
    )
    history_text = format_cli_tool_history(
        all_results,
        original_input=original_input,
        max_chars=max_chars,
        max_total_chars=history_budget,
    )
    combined = "\n\n".join([current_text, history_text, completion_text])
    # Defensive final clamp for unusually long control/objective strings or
    # malformed user settings.  The recent current-round context is retained
    # ahead of older history by the budgeting above.
    if len(combined) > context_limit:
        # ``clip_text`` intentionally appends a human-readable suffix and can
        # therefore exceed its nominal limit.  Follow-up context is a hard
        # provider-input budget, so use a strict final clamp here.
        return combined[:context_limit]
    return combined


CLI_TOOL_COMPLETION_CONTROL_PROMPT = """Completion control after tool results:
- After receiving tool results, first decide whether the original user request is already satisfied.
- If satisfied, output the final answer immediately and do not call another tool.
- Call another tool only when the previous result is missing, failed, contradictory, stale, or the original request still has unfinished subgoals.
- A new tool call is also allowed when the previous result created a concrete new target to inspect, or when a mutation was made and post-change verification is still incomplete.
- Do not call another tool just to reconfirm a successful result.
- Re-running the same tool is allowed only when a concrete new reason exists, such as verifying a mutation, checking a changed file, resolving an error, or completing a distinct remaining subtask."""


def _cli_tool_call_signature(call: UnifiedToolCall) -> tuple[str, str]:
    return (
        call.tool,
        json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str),
    )


def _redundant_successful_recall_batch(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> list[UnifiedToolResult] | None:
    """Match an exact call batch against the successful ledger suffix."""

    if not calls or len(calls) > len(all_results):
        return None
    suffix = list(all_results[-len(calls) :])
    for call, previous in zip(calls, suffix):
        if _cli_tool_call_signature(previous.call) != _cli_tool_call_signature(call):
            return None
        if not previous.success or not previous.output.strip():
            return None
    return suffix


def _is_redundant_successful_cli_recall(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> bool:
    return _redundant_successful_recall_batch(calls, all_results) is not None


def _redundant_cli_recall_final_output(
    calls: Sequence[UnifiedToolCall],
    all_results: Sequence[UnifiedToolResult],
) -> str:
    outputs: list[str] = []
    for call in calls:
        signature = _cli_tool_call_signature(call)
        for previous in reversed(all_results):
            if _cli_tool_call_signature(previous.call) == signature and previous.success:
                outputs.append(previous.output)
                break
    return "\n".join(output for output in outputs if output).strip()


def run_openai_compatible_turn_loop(
    *,
    initial_messages: list[dict[str, Any]],
    assistant_message: Any,
    api_kwargs: dict[str, Any],
    registry: ToolRegistry,
    create_completion: Callable[[dict[str, Any]], Any],
    log_prefix: str = "UnifiedTurn",
    max_rounds: int = 5,
    max_tool_result_chars: int | None = None,
    message_content: Callable[[Any], str] | None = None,
    config: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    final_response_check: (
        Callable[[str, Sequence[UnifiedToolResult], int], str | None] | None
    ) = None,
    skip_final_response_check_on_empty: bool = False,
    event_callback: SyncStreamEmitter | None = None,
    turn_ledger: UnifiedTurnLedger | None = None,
    restore_tool_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> UnifiedTurnResult:
    """Run a Codex-style OpenAI-compatible tool loop.

    ``event_callback`` が渡された場合だけ、ツール往復ラウンドの通常テキストを
    ``assistant_text``、reasoning を ``thinking`` として同期発行する。
    """

    effective_user_input = user_input or _last_user_message_content(initial_messages)
    # ``id(message)`` はオブジェクト解放後にアドレスが再利用されるため、
    # 別メッセージを無言でスキップしうる。round index をキーにして誤爆を避ける。
    emitted_thinking_rounds: set[int] = set()

    def _emit_thinking_once(message: Any, round_index: int) -> None:
        """同じラウンドについて thinking を二重発行しない。"""
        if event_callback is None or message is None:
            return
        marker = int(round_index)
        if marker in emitted_thinking_rounds:
            return
        emitted_thinking_rounds.add(marker)
        emit_thinking(
            event_callback,
            thinking_text_from_message(message),
            round_index=round_index,
        )

    router = RegistryToolRouter(
        registry,
        log_prefix=log_prefix,
        config=config,
        user_input=effective_user_input,
        enforce_tool_policy=enforce_tool_policy,
    )
    current_messages = list(initial_messages)
    current_message = assistant_message
    current_tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
    current_content = openai_message_content(assistant_message, message_content)
    ledger = turn_ledger or _ACTIVE_TURN_LEDGER.get() or UnifiedTurnLedger()
    all_results = ledger.results
    display_results = list(all_results)

    def _restore_arguments_for_tool(
        callback: Callable[..., dict[str, Any]],
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Pass the tool name when supported, retaining legacy callbacks."""

        try:
            restored = callback(dict(arguments), tool_name=tool_name)
        except TypeError:
            # Embedders may still provide a text-only one-argument callback.
            restored = callback(dict(arguments))
        # A callback may intentionally return ``None`` when no alias
        # restoration is needed.  Keep the provider's original arguments
        # instead of converting None to a TypeError.
        if restored is None:
            return dict(arguments)
        try:
            return dict(restored)
        except (TypeError, ValueError):
            return dict(arguments)

    for round_index in range(1, max_rounds + 1):
        # native_runtime と揃えて round は 0 始まりで配信する。
        emit_round = round_index - 1
        _emit_thinking_once(current_message, emit_round)
        if not current_tool_calls:
            continuation_prompt = (
                final_response_check(current_content, all_results, round_index)
                if final_response_check
                and not (skip_final_response_check_on_empty and not current_content.strip())
                else None
            )
            if continuation_prompt:
                current_messages.append(serialize_openai_assistant_message(current_message))
                current_messages.append(
                    {"role": "user", "content": continuation_prompt}
                )
                follow_up_kwargs = dict(api_kwargs)
                follow_up_kwargs["messages"] = current_messages
                if "tool_choice" in follow_up_kwargs:
                    follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                        follow_up_kwargs["tool_choice"]
                    )
                response = create_completion(follow_up_kwargs)
                message = response.choices[0].message
                current_message = message
                current_tool_calls = list(getattr(message, "tool_calls", None) or [])
                current_content = openai_message_content(message, message_content)
                continue
            return UnifiedTurnResult(
                final_output=current_content,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="final",
                audit_tool_results=list(all_results),
                messages=[
                    *current_messages,
                    serialize_openai_assistant_message(current_message),
                ],
            )

        # ツール呼び出しを伴うラウンドの通常テキストは途中経過として配信する。
        emit_assistant_text(event_callback, current_content, round_index=emit_round)

        current_messages.append(serialize_openai_assistant_message(current_message))

        calls = [
            UnifiedToolCall(
                tool=name,
                arguments=(
                    _restore_arguments_for_tool(
                        restore_tool_arguments,
                        name,
                        dict(arguments),
                    )
                    if restore_tool_arguments is not None
                    else arguments
                ),
                call_id=openai_tool_call_id(raw_call),
                raw=raw_call,
            )
            for raw_call in current_tool_calls
            for name, arguments in [openai_tool_call_function(raw_call)]
        ]
        ledger_snapshot = tuple(all_results)
        redundant_batch = _redundant_successful_recall_batch(
            calls,
            ledger_snapshot,
        )
        results: list[UnifiedToolResult] = []
        model_results: list[UnifiedToolResult] = []
        suppressed_results: list[UnifiedToolResult] = []
        for call_index, call in enumerate(calls):
            previous = (
                redundant_batch[call_index]
                if redundant_batch is not None
                else None
            )
            if previous is not None:
                suppressed = UnifiedToolResult(
                    call=call,
                    output=previous.output,
                    success=True,
                    elapsed_ms=previous.elapsed_ms,
                )
                model_results.append(suppressed)
                suppressed_results.append(suppressed)
                logger.info(
                    "[%s] Suppressed redundant successful OpenAI tool recall: %s",
                    log_prefix,
                    call.tool,
                )
                continue

            operation_id = call.call_id.strip() or str(uuid.uuid4())
            _emit_sync(
                event_callback,
                "tool_start",
                {
                    "tool": call.tool,
                    "tool_args": dict(call.arguments or {}),
                    "message": f"Running {call.tool}",
                    "operation_id": operation_id,
                },
            )
            result = router.execute(call)
            results.append(result)
            model_results.append(result)
            ledger.record(result)
            display_results.append(result)
            _emit_sync(
                event_callback,
                "tool_end",
                {
                    "tool": call.tool,
                    "tool_args": dict(call.arguments or {}),
                    "message": f"Completed {call.tool}",
                    "operation_id": operation_id,
                },
            )

        for result in model_results:
            model_payload = model_tool_result_payload(
                tool_name=result.call.tool,
                output=result.model_output,
                user_input=effective_user_input,
                max_chars=max_tool_result_chars,
                config=config,
                legacy_clip=clip_text,
            )
            current_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call.call_id,
                    "content": model_payload.text,
                }
            )

        if suppressed_results and not results:
            final_kwargs = dict(api_kwargs)
            final_kwargs["messages"] = current_messages
            final_kwargs.pop("tools", None)
            final_kwargs.pop("tool_choice", None)
            response = create_completion(final_kwargs)
            message = response.choices[0].message
            final_content = openai_message_content(message, message_content)
            _emit_thinking_once(message, emit_round + 1)
            repeated_tool_calls = list(getattr(message, "tool_calls", None) or [])
            if repeated_tool_calls:
                final_content = (
                    "The requested tool operation was already completed; "
                    "no duplicate execution was performed."
                )
                final_message = {"role": "assistant", "content": final_content}
            else:
                final_message = serialize_openai_assistant_message(message)
            return UnifiedTurnResult(
                final_output=final_content,
                tool_results=display_results,
                rounds=round_index,
                stopped_reason=(
                    "redundant_tool_call_repeated"
                    if repeated_tool_calls
                    else "redundant_tool_call_suppressed"
                ),
                messages=[
                    *current_messages,
                    final_message,
                ],
                audit_tool_results=list(all_results),
            )

        follow_up_kwargs = dict(api_kwargs)
        follow_up_kwargs["messages"] = current_messages
        if "tool_choice" in follow_up_kwargs:
            follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                follow_up_kwargs["tool_choice"]
            )

        response = create_completion(follow_up_kwargs)
        message = response.choices[0].message
        current_message = message
        next_tool_calls = getattr(message, "tool_calls", None)
        current_content = openai_message_content(message, message_content)
        # 次ラウンドへ進む場合はループ先頭の重複排除で1回だけ配信される。
        _emit_thinking_once(current_message, emit_round + 1)
        if next_tool_calls:
            current_tool_calls = list(next_tool_calls)
            continue
        continuation_prompt = (
            final_response_check(current_content, all_results, round_index)
            if final_response_check
            and not (skip_final_response_check_on_empty and not current_content.strip())
            else None
        )
        if continuation_prompt:
            current_messages.append(serialize_openai_assistant_message(current_message))
            current_messages.append({"role": "user", "content": continuation_prompt})
            follow_up_kwargs = dict(api_kwargs)
            follow_up_kwargs["messages"] = current_messages
            if "tool_choice" in follow_up_kwargs:
                follow_up_kwargs["tool_choice"] = _follow_up_tool_choice(
                    follow_up_kwargs["tool_choice"]
                )
            response = create_completion(follow_up_kwargs)
            message = response.choices[0].message
            current_message = message
            current_tool_calls = list(getattr(message, "tool_calls", None) or [])
            current_content = openai_message_content(message, message_content)
            # 継続要求で差し替えた新しいメッセージは同じ round 番号のまま
            # 次ループ先頭へ渡るため、その round の発行済みマークを外して
            # 新しい思考が落ちないようにする。
            emitted_thinking_rounds.discard(emit_round + 1)
            continue

        return UnifiedTurnResult(
            final_output=current_content,
            tool_results=display_results,
            rounds=round_index,
            stopped_reason="final",
            messages=[
                *current_messages,
                serialize_openai_assistant_message(current_message),
            ],
            audit_tool_results=list(all_results),
        )

    logger.warning("[%s] Unified turn loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=current_content or "",
        tool_results=display_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
        messages=[
            *current_messages,
            serialize_openai_assistant_message(current_message),
        ],
        audit_tool_results=list(all_results),
    )


def run_cli_tool_call_loop(
    *,
    original_input: str,
    initial_output: str,
    registry: ToolRegistry,
    parse_tool_calls: Callable[[str], list[dict[str, Any]]],
    execute_follow_up: Callable[[str], tuple[bool, str]],
    build_follow_up_prompt: Callable[[str, str, str], str],
    log_prefix: str = "UnifiedCLITurn",
    # Explicit callers (the main CLI client) provide profile-specific
    # budgets.  Specialist CLI delegation uses this default, so keep it at
    # the bounded work budget rather than the old chat-sized five rounds.
    max_rounds: int = 12,
    max_tool_result_chars: int | None = None,
    config: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    final_response_check: (
        Callable[[str, Sequence[UnifiedToolResult], int], str | None] | None
    ) = None,
    should_cancel: Callable[[], bool] | None = None,
) -> UnifiedTurnResult:
    """Run a CLI backend tool loop with repeated follow-up support."""

    router = RegistryToolRouter(
        registry,
        log_prefix=log_prefix,
        config=config,
        user_input=user_input or original_input,
        enforce_tool_policy=enforce_tool_policy,
    )
    current_output = str(initial_output or "")
    all_results: list[UnifiedToolResult] = []
    display_results: list[UnifiedToolResult] = []

    # ``max_rounds`` limits *tool executions*, not the follow-up response
    # that is produced after the last tool result.  Keep one parse-only pass
    # after the final allowed tool round so a normal final answer is not
    # misclassified as exhausted.  The extra pass must never execute another
    # tool call: a pending marker at that point is an explicit exhaustion.
    for round_index in range(1, max_rounds + 2):
        if should_cancel and should_cancel():
            return UnifiedTurnResult(
                final_output=current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="cancelled",
                audit_tool_results=list(all_results),
            )
        parsed_calls = parse_tool_calls(current_output)
        if not parsed_calls:
            if round_index > max_rounds:
                continuation_prompt = (
                    final_response_check(current_output, all_results, round_index)
                    if final_response_check
                    else None
                )
                if continuation_prompt:
                    logger.warning(
                        "[%s] CLI final response still requires a tool after max rounds",
                        log_prefix,
                    )
                    return UnifiedTurnResult(
                        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                        tool_results=display_results,
                        rounds=max_rounds,
                        stopped_reason="max_rounds",
                        audit_tool_results=list(all_results),
                    )
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=max_rounds,
                    stopped_reason="final",
                    audit_tool_results=list(all_results),
                )
            continuation_prompt = (
                final_response_check(current_output, all_results, round_index)
                if final_response_check
                else None
            )
            if continuation_prompt:
                follow_up = build_follow_up_prompt(
                    original_input,
                    current_output,
                    continuation_prompt,
                )
                _emit_sync(
                    event_callback,
                    "status_update",
                    {
                        "status": "cli_tool_required",
                        "message": "Required tool check requested a CLI follow-up",
                    },
                )
                success, follow_up_output = execute_follow_up(follow_up)
                if not success:
                    logger.error(
                        "[%s] CLI required-tool follow-up failed: %s",
                        log_prefix,
                        follow_up_output,
                    )
                    return UnifiedTurnResult(
                        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                        tool_results=display_results,
                        rounds=round_index - 1,
                        stopped_reason="follow_up_error",
                        audit_tool_results=list(all_results),
                    )
                current_output = str(follow_up_output or "")
                continue
            return UnifiedTurnResult(
                final_output=current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="final",
                audit_tool_results=list(all_results),
            )

        if round_index > max_rounds:
            # The model returned another tool marker after the last permitted
            # round.  Do not execute it; the caller must surface a terminal
            # exhausted state rather than treating the marker as an answer.
            logger.warning(
                "[%s] CLI tool loop exceeded max rounds with a pending tool call",
                log_prefix,
            )
            return UnifiedTurnResult(
                final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                tool_results=display_results,
                rounds=max_rounds,
                stopped_reason="max_rounds",
                audit_tool_results=list(all_results),
            )

        calls = cli_tool_calls_from_parsed(parsed_calls)
        if _is_redundant_successful_cli_recall(calls, all_results):
            final_output = _redundant_cli_recall_final_output(calls, all_results)
            logger.info(
                "[%s] Suppressed redundant successful CLI tool recall",
                log_prefix,
            )
            return UnifiedTurnResult(
                final_output=final_output or current_output,
                tool_results=display_results,
                rounds=round_index - 1,
                stopped_reason="redundant_tool_call_suppressed",
                audit_tool_results=list(all_results),
            )
        logger.info(
            "[%s] Executing %s CLI tool call(s)",
            log_prefix,
            len(calls),
        )
        results: list[UnifiedToolResult] = []
        for call in calls:
            if should_cancel and should_cancel():
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=round_index - 1,
                    stopped_reason="cancelled",
                    audit_tool_results=list(all_results),
                )
            _emit_sync(
                event_callback,
                "tool_start",
                {
                    "tool": call.tool,
                    "tool_args": dict(call.arguments or {}),
                    "message": f"Running {call.tool}",
                },
            )
            result = router.execute(call)
            results.append(result)
            display_results.append(result)
            if should_cancel and should_cancel():
                return UnifiedTurnResult(
                    final_output=current_output,
                    tool_results=display_results,
                    rounds=round_index,
                    stopped_reason="cancelled",
                    audit_tool_results=[*all_results, *results],
                )
            _emit_sync(
                event_callback,
                "tool_end",
                {
                    "tool": call.tool,
                    "tool_args": dict(call.arguments or {}),
                    "message": f"Completed {call.tool}",
                    "tool_result": {
                        "tool": call.tool,
                        "arguments": dict(call.arguments or {}),
                        "output": result.model_output,
                        "error": result.error if not result.success else "",
                    },
                },
            )
        all_results.extend(results)

        follow_up = build_follow_up_prompt(
            original_input,
            current_output,
            format_cli_follow_up_tool_context(
                results,
                all_results,
                original_input=original_input,
                max_chars=max_tool_result_chars,
                config=config,
                user_input=user_input or original_input,
            ),
        )
        _emit_sync(
            event_callback,
            "status_update",
            {
                "status": "cli_tool_results_received",
                "message": "Tool results received; continuing CLI turn",
            },
        )
        success, follow_up_output = execute_follow_up(follow_up)
        if not success:
            logger.error(
                "[%s] CLI follow-up failed: %s",
                log_prefix,
                follow_up_output,
            )
            return UnifiedTurnResult(
                final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
                tool_results=display_results,
                rounds=round_index,
                stopped_reason="follow_up_error",
                audit_tool_results=list(all_results),
            )

        current_output = str(follow_up_output or "")

    logger.warning("[%s] CLI tool loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=CLI_TOOL_LOOP_FAILURE_MESSAGE,
        tool_results=display_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
        audit_tool_results=list(all_results),
    )


def _emit_sync(
    event_callback: Callable[[str, dict[str, Any]], Any] | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_callback is None:
        return
    try:
        event_callback(event_type, data)
    except GenerationInterrupted:
        raise
    except Exception:
        logger.debug(
            "[UnifiedCLITurn] Event callback failed for %s",
            event_type,
            exc_info=True,
        )


def _last_user_message_content(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            ]
            return "\n".join(part for part in parts if part).strip()
    return ""
