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
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..tools.registry import ToolRegistry
from .context_compression import model_tool_result_payload

logger = logging.getLogger(__name__)


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

    @property
    def tool_calls(self) -> list[UnifiedToolCall]:
        return [result.call for result in self.tool_results]


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
    ):
        self.registry = registry
        self.log_prefix = log_prefix
        self.config = config
        self.user_input = user_input
        self.enforce_tool_policy = enforce_tool_policy

    def _policy_block_result(self, call: UnifiedToolCall) -> UnifiedToolResult | None:
        if not self.enforce_tool_policy:
            return None
        if self.config is None and call.tool == "search_memory":
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
            logger.debug("[%s] Tool policy check failed", self.log_prefix, exc_info=True)
            return None

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
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return blocked
        blocked = self._approval_block_result(call)
        if blocked is not None:
            return blocked

        started = time.perf_counter()
        try:
            result = self.registry.execute(call.tool, **dict(call.arguments or {}))
            output = str(result)
            success = True
            error = ""
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
        return UnifiedToolResult(
            call=call,
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
        )

    async def execute_async(self, call: UnifiedToolCall) -> UnifiedToolResult:
        blocked = self._policy_block_result(call)
        if blocked is not None:
            return blocked
        blocked = await self._approval_block_result_async(call)
        if blocked is not None:
            return blocked

        started = time.perf_counter()
        try:
            result = await self.registry.execute_async(
                call.tool,
                **dict(call.arguments or {}),
            )
            output = str(result)
            success = True
            error = ""
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
        return UnifiedToolResult(
            call=call,
            output=output,
            success=success,
            error=error,
            elapsed_ms=elapsed_ms,
        )

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
) -> UnifiedTurnResult:
    """Run a Codex-style OpenAI-compatible tool loop."""

    effective_user_input = user_input or _last_user_message_content(initial_messages)
    router = RegistryToolRouter(
        registry,
        log_prefix=log_prefix,
        config=config,
        user_input=effective_user_input,
        enforce_tool_policy=enforce_tool_policy,
    )
    current_messages = list(initial_messages)
    current_tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
    current_content = openai_message_content(assistant_message, message_content)
    all_results: list[UnifiedToolResult] = []

    for round_index in range(1, max_rounds + 1):
        if not current_tool_calls:
            continuation_prompt = (
                final_response_check(current_content, all_results, round_index)
                if final_response_check
                else None
            )
            if continuation_prompt:
                current_messages.append(
                    {"role": "assistant", "content": current_content or ""}
                )
                current_messages.append(
                    {"role": "user", "content": continuation_prompt}
                )
                follow_up_kwargs = dict(api_kwargs)
                follow_up_kwargs["messages"] = current_messages
                if follow_up_kwargs.get("tool_choice") == "required":
                    follow_up_kwargs["tool_choice"] = "auto"
                response = create_completion(follow_up_kwargs)
                message = response.choices[0].message
                current_tool_calls = list(getattr(message, "tool_calls", None) or [])
                current_content = openai_message_content(message, message_content)
                continue
            return UnifiedTurnResult(
                final_output=current_content,
                tool_results=all_results,
                rounds=round_index - 1,
                stopped_reason="final",
            )

        current_messages.append(
            {
                "role": "assistant",
                "content": current_content or "",
                "tool_calls": [
                    serialize_openai_tool_call(tool_call)
                    for tool_call in current_tool_calls
                ],
            }
        )

        calls = [
            UnifiedToolCall(
                tool=name,
                arguments=arguments,
                call_id=openai_tool_call_id(raw_call),
                raw=raw_call,
            )
            for raw_call in current_tool_calls
            for name, arguments in [openai_tool_call_function(raw_call)]
        ]
        results = router.execute_many(calls)
        all_results.extend(results)

        for result in results:
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

        follow_up_kwargs = dict(api_kwargs)
        follow_up_kwargs["messages"] = current_messages
        if follow_up_kwargs.get("tool_choice") == "required":
            follow_up_kwargs["tool_choice"] = "auto"

        response = create_completion(follow_up_kwargs)
        message = response.choices[0].message
        next_tool_calls = getattr(message, "tool_calls", None)
        current_content = openai_message_content(message, message_content)
        if next_tool_calls:
            current_tool_calls = list(next_tool_calls)
            continue
        continuation_prompt = (
            final_response_check(current_content, all_results, round_index)
            if final_response_check
            else None
        )
        if continuation_prompt:
            current_messages.append(
                {"role": "assistant", "content": current_content or ""}
            )
            current_messages.append({"role": "user", "content": continuation_prompt})
            follow_up_kwargs = dict(api_kwargs)
            follow_up_kwargs["messages"] = current_messages
            if follow_up_kwargs.get("tool_choice") == "required":
                follow_up_kwargs["tool_choice"] = "auto"
            response = create_completion(follow_up_kwargs)
            message = response.choices[0].message
            current_tool_calls = list(getattr(message, "tool_calls", None) or [])
            current_content = openai_message_content(message, message_content)
            continue

        return UnifiedTurnResult(
            final_output=current_content,
            tool_results=all_results,
            rounds=round_index,
            stopped_reason="final",
        )

    logger.warning("[%s] Unified turn loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=current_content or "",
        tool_results=all_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
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
    max_rounds: int = 5,
    max_tool_result_chars: int | None = None,
    config: Any | None = None,
    user_input: str | None = None,
    enforce_tool_policy: bool = True,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    final_response_check: (
        Callable[[str, Sequence[UnifiedToolResult], int], str | None] | None
    ) = None,
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

    for round_index in range(1, max_rounds + 1):
        parsed_calls = parse_tool_calls(current_output)
        if not parsed_calls:
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
                        final_output=current_output,
                        tool_results=all_results,
                        rounds=round_index - 1,
                        stopped_reason="follow_up_error",
                    )
                current_output = str(follow_up_output or "")
                continue
            return UnifiedTurnResult(
                final_output=current_output,
                tool_results=all_results,
                rounds=round_index - 1,
                stopped_reason="final",
            )

        calls = cli_tool_calls_from_parsed(parsed_calls)
        logger.info(
            "[%s] Executing %s CLI tool call(s)",
            log_prefix,
            len(calls),
        )
        results: list[UnifiedToolResult] = []
        for call in calls:
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
            format_cli_tool_results(
                results,
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
                final_output=current_output,
                tool_results=all_results,
                rounds=round_index,
                stopped_reason="follow_up_error",
            )

        current_output = str(follow_up_output or "")

    logger.warning("[%s] CLI tool loop exceeded max rounds", log_prefix)
    return UnifiedTurnResult(
        final_output=current_output,
        tool_results=all_results,
        rounds=max_rounds,
        stopped_reason="max_rounds",
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
