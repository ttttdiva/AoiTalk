"""AoiTalk-native agent runtime.

The runtime owns the model/tool loop instead of delegating it to a provider SDK.
Provider clients are transport details; tools, retries, and callback events stay
inside AoiTalk.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

from openai import AsyncOpenAI

from ..tools.core import ToolDefinition, ensure_tool_definition
from ..tools.registry import ToolRegistry
from .context_compression import model_tool_result_payload
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


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


class AgentTurnRunner:
    """Run a single AoiTalk agent turn with OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        provider_label: str = "openai",
        max_tool_rounds: int = 6,
        max_tool_result_chars: int = 12000,
        config: Any | None = None,
    ) -> None:
        self.client = client
        self.provider_label = provider_label
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_result_chars = max_tool_result_chars
        self.config = config

    async def run(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> NativeRunResult:
        await _emit(stream_callback, "stream_start", {"message": "応答を生成しています"})

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.instructions or ""},
            {"role": "user", "content": user_input or ""},
        ]
        plain_user_input = (
            user_input
            if isinstance(user_input, str)
            else "\n".join(
                str(part.get("text") or "")
                for part in user_input
                if isinstance(part, dict) and part.get("type") == "text"
            )
        )
        tool_records: list[ToolExecutionRecord] = []
        tool_registry = ToolRegistry()
        for tool in agent.tools:
            tool_registry.register(tool)
        tool_router = RegistryToolRouter(
            tool_registry,
            log_prefix=f"NativeAgentTurnRunner:{agent.name}",
            config=self.config,
        )
        tools_payload = _tool_specs(agent.tools)
        requested_tool_choice = _normalize_tool_choice(
            agent.model_settings.tool_choice,
            has_tools=bool(tools_payload),
        )
        current_tool_choice = requested_tool_choice
        final_output = ""

        for round_index in range(self.max_tool_rounds + 1):
            kwargs = self._build_completion_kwargs(
                agent=agent,
                messages=messages,
                tools_payload=tools_payload,
                tool_choice=current_tool_choice,
            )
            response = await self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message
            content = str(getattr(message, "content", "") or "")
            final_output = content
            tool_calls = list(getattr(message, "tool_calls", None) or [])

            if not tool_calls:
                if content:
                    await _emit(stream_callback, "stream_token", {"content": content})
                await _emit(stream_callback, "stream_end", {"content": content})
                return NativeRunResult(
                    final_output=content,
                    messages=list(messages),
                    tool_calls=list(tool_records),
                )

            messages.append(_assistant_message_payload(content, tool_calls))
            for tool_call in tool_calls:
                tool_name = _tool_call_name(tool_call)
                call_id = _tool_call_id(tool_call)
                args, parse_error = _tool_call_arguments(tool_call)
                await _emit(
                    stream_callback,
                    "tool_start",
                    {
                        "tool": tool_name,
                        "tool_args": args,
                        "message": f"{tool_name} を実行しています",
                    },
                )

                if parse_error:
                    result_text = f"Error: invalid JSON arguments: {parse_error}"
                else:
                    tool_result = await tool_router.execute_async(
                        UnifiedToolCall(
                            tool=tool_name,
                            arguments=args,
                            call_id=call_id,
                        )
                    )
                    result_text = tool_result.model_output

                model_payload = model_tool_result_payload(
                    tool_name=tool_name,
                    output=result_text,
                    user_input=plain_user_input,
                    max_chars=self.max_tool_result_chars,
                    config=self.config,
                    legacy_clip=_clip_text,
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool=tool_name,
                        arguments=dict(args),
                        result=result_text,
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": model_payload.text,
                    }
                )
                await _emit(
                    stream_callback,
                    "tool_end",
                    {
                        "tool": tool_name,
                        "tool_args": args,
                        "message": "ツール実行が完了しました",
                    },
                )

            if round_index == 0 and current_tool_choice == "required":
                current_tool_choice = "auto"

        fallback = final_output or "ツール実行後の最終応答を生成できませんでした。"
        await _emit(stream_callback, "stream_token", {"content": fallback})
        await _emit(stream_callback, "stream_end", {"content": fallback})
        logger.warning("Native agent turn exceeded max tool rounds: %s", agent.name)
        return NativeRunResult(
            final_output=fallback,
            messages=list(messages),
            tool_calls=list(tool_records),
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
            "messages": messages,
        }
        if tools_payload:
            kwargs["tools"] = tools_payload
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        effort = getattr(agent.model_settings.reasoning, "effort", None)
        if effort and self.provider_label == "openai":
            kwargs["reasoning_effort"] = effort
        return kwargs

def create_async_openai_client(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    default_headers: Optional[dict[str, str]] = None,
) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {"api_key": api_key or os.getenv("OPENAI_API_KEY")}
    if base_url:
        kwargs["base_url"] = base_url
    if default_headers:
        kwargs["default_headers"] = default_headers
    return AsyncOpenAI(**kwargs)


async def run_native_agent_once(
    agent: AgentDefinition,
    prompt: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    default_headers: Optional[dict[str, str]] = None,
    provider_label: str = "openai",
    config: Any | None = None,
) -> NativeRunResult:
    runner = AgentTurnRunner(
        client=create_async_openai_client(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
        ),
        provider_label=provider_label,
        config=config,
    )
    return await runner.run(agent, prompt)


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


def _normalize_tool_choice(value: Optional[str], *, has_tools: bool) -> Optional[str]:
    if not has_tools:
        return None
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "required", "none"}:
        return normalized
    return "auto"


def _assistant_message_payload(content: str, tool_calls: list[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [_serialize_tool_call(tool_call) for tool_call in tool_calls],
    }


def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
    return {
        "id": _tool_call_id(tool_call),
        "type": "function",
        "function": {
            "name": _tool_call_name(tool_call),
            "arguments": _tool_call_raw_arguments(tool_call),
        },
    }


def _tool_call_id(tool_call: Any) -> str:
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
