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
from .conversation_context import (
    PromptMessages,
    ProviderState,
    normalize_usage,
    prompt_text,
    stable_cache_key,
    stable_tool_schemas,
)
from .context_budget import resolve_context_budget
from .context_snapshot import component, message_components, reconcile_snapshot, snapshot, tool_components, without_text
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _normalized_usage(usage: Any, *, provider: str = "") -> dict[str, Any] | None:
    normalized = normalize_usage(usage, provider=provider)
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
        "cache_write_tokens",
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
        self.conversation_state_mode = "stateless"
        self.provider_state = ProviderState()
        self.prompt_cache_key: str | None = None
        self.prompt_cache_retention: str | None = None
        try:
            resolved_budget = resolve_context_budget(
                config=config,
                provider_key=provider_label,
                base_url=str(getattr(client, "base_url", "") or ""),
                model_name=None,
            )
            self.context_budget = resolved_budget if resolved_budget.source != "fallback" else None
        except Exception:
            self.context_budget = None

    async def run(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> NativeRunResult:
        from ..services.project_context import get_runtime_project_context
        from ..services.workspace_git_service import auto_checkpoint_if_changed, tracked_workspace_fingerprint

        project_context = get_runtime_project_context() or {}
        project_id = str(project_context.get("id") or "") or None
        try:
            before = tracked_workspace_fingerprint(project_id) if project_id else None
        except Exception:  # noqa: BLE001
            logger.warning("workspace checkpoint snapshot に失敗しました", exc_info=True)
            before = None
        try:
            return await self._run_core(agent, user_input, stream_callback=stream_callback)
        finally:
            # A tool may have written files before a later model/provider failure.
            auto_checkpoint_if_changed(project_id, before)

    async def _run_core(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> NativeRunResult:
        # 公式 OpenAI 経路は Responses API を使う。openrouter など base_url を差し替えた
        # OpenAI 互換プロバイダは従来の chat.completions を維持する。
        if self.provider_label == "openai":
            return await self._run_core_responses(
                agent, user_input, stream_callback=stream_callback
            )
        return await self._run_core_chat(
            agent, user_input, stream_callback=stream_callback
        )

    async def _run_core_chat(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> NativeRunResult:
        await _emit(stream_callback, "stream_start", {"message": "応答を生成しています"})

        seed_messages = _prompt_messages_or_user(user_input)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.instructions or ""},
            *seed_messages,
        ]
        plain_user_input = prompt_text(user_input)
        tool_records: list[ToolExecutionRecord] = []
        usage_records: list[dict[str, int]] = []
        context_snapshots: list[dict[str, Any]] = []
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
            request_snapshot = snapshot(
                provider=self.provider_label,
                model=agent.model,
                components=[
                    *message_components(without_text(kwargs.get("messages", []), getattr(self, "snapshot_rendered_bundle", ""))),
                    *list(getattr(self, "snapshot_bundle_components", []) or []),
                    *tool_components(kwargs.get("tools", []), source="chat.completions tools payload"),
                ],
                request_index=len(context_snapshots),
                request_kind="chat.completions",
                context_window_tokens=self.context_budget.context_window_tokens if self.context_budget else None,
                response_tokens=self.context_budget.response_tokens if self.context_budget else None,
                window_source=self.context_budget.source if self.context_budget else None,
            )
            context_snapshots.append(request_snapshot)
            response = await self.client.chat.completions.create(**kwargs)
            usage = _normalized_usage(
                getattr(response, "usage", None), provider=self.provider_label
            )
            if usage:
                usage_records.append(usage)
                context_snapshots[-1] = reconcile_snapshot(request_snapshot, usage.get("input_tokens"))
            choice = response.choices[0]
            message = choice.message
            content = str(getattr(message, "content", "") or "")
            final_output = content
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            assistant_payload = _assistant_message_payload(message)

            if not tool_calls:
                if content:
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
        result = NativeRunResult(
            final_output=fallback,
            messages=[
                *messages,
                {"role": "assistant", "content": fallback},
            ],
            tool_calls=list(tool_records),
            usage_records=list(usage_records),
            context_snapshots=list(context_snapshots),
        )
        return result

    async def _run_core_responses(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> NativeRunResult:
        """公式 OpenAI Responses API でエージェントターンを実行する。

        gpt-5.6-terra 系は chat.completions では function tools と reasoning_effort を
        併用できないため、function tools を使う経路はすべて Responses API に寄せる。
        会話状態はサーバー保存に依存せず、毎ラウンド input を全量送信する（store=False）。
        """
        await _emit(stream_callback, "stream_start", {"message": "応答を生成しています"})

        plain_user_input = prompt_text(user_input)

        # Responses API へ渡す input items（毎ラウンド全量送信する状態）。
        seed_messages = _prompt_messages_or_user(user_input)
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
        tool_registry = ToolRegistry()
        for tool in agent.tools:
            tool_registry.register(tool)
        tool_router = RegistryToolRouter(
            tool_registry,
            log_prefix=f"NativeAgentTurnRunner:{agent.name}",
            config=self.config,
        )
        tools_payload = _responses_tool_specs(agent.tools)
        requested_tool_choice = _normalize_tool_choice(
            agent.model_settings.tool_choice,
            has_tools=bool(tools_payload),
        )
        current_tool_choice = requested_tool_choice
        effort = getattr(agent.model_settings.reasoning, "effort", None)
        final_output = ""

        for round_index in range(self.max_tool_rounds + 1):
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
                kwargs["reasoning"] = {"effort": effort}
            if self.config and hasattr(self.config, "get"):
                max_output_tokens = self.config.get(
                    "free_team.max_output_tokens", None
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

            request_components = [
                component("system_instructions", "System instructions", kwargs.get("instructions"), source="responses instructions"),
                component("current_user_message" if len(input_items) == 1 else "conversation_history", "Current user message" if len(input_items) == 1 else "Conversation history", str(kwargs.get("input")).replace(getattr(self, "snapshot_rendered_bundle", ""), "", 1) if getattr(self, "snapshot_rendered_bundle", "") else kwargs.get("input"), source="responses input"),
                *list(getattr(self, "snapshot_bundle_components", []) or []),
                *tool_components(kwargs.get("tools", []), source="responses tools payload"),
            ]
            if len(input_items) > 1:
                request_components.append(component("provider_managed", "Provider-managed context", source="responses encrypted reasoning", measurement="unavailable", preview="暗号化されたreasoning itemの詳細は取得不能"))
            request_snapshot = snapshot(
                provider=self.provider_label,
                model=agent.model,
                components=request_components,
                request_index=len(context_snapshots),
                request_kind="responses",
                context_window_tokens=self.context_budget.context_window_tokens if self.context_budget else None,
                response_tokens=self.context_budget.response_tokens if self.context_budget else None,
                window_source=self.context_budget.source if self.context_budget else None,
            )
            context_snapshots.append(request_snapshot)

            try:
                response = await self.client.responses.create(**kwargs)
            except Exception:
                if self.conversation_state_mode != "provider-managed":
                    raise
                # Provider state may expire or be unavailable after a server
                # restart.  Rebuild from AoiTalk's canonical transcript.
                logger.warning("provider-managed stateを破棄してstatelessへフォールバック", exc_info=True)
                self.conversation_state_mode = "stateless"
                self.provider_state.reset()
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("previous_response_id", None)
                retry_kwargs["store"] = False
                retry_kwargs["input"] = list(stateless_input_items)
                response = await self.client.responses.create(**retry_kwargs)
            usage = _normalized_usage(
                getattr(response, "usage", None), provider=self.provider_label
            )
            if usage:
                usage_records.append(usage)
                context_snapshots[-1] = reconcile_snapshot(request_snapshot, usage.get("input_tokens"))
            output_items = list(getattr(response, "output", None) or [])
            function_calls = _responses_function_calls(output_items)
            content = responses_output_text(response, output_items)
            final_output = content
            response_id = getattr(response, "id", None)
            if response_id is None and isinstance(response, dict):
                response_id = response.get("id")
            if (
                self.conversation_state_mode == "provider-managed"
                and response_id
            ):
                self.provider_state.previous_response_id = str(response_id)

            if not function_calls:
                if content:
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
            serialized_output_items = _serialize_responses_output_items(output_items)
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
                        _responses_call_to_chat_tool_call(fc) for fc in function_calls
                    ],
                }
            )

            for function_call in function_calls:
                tool_name = _responses_call_name(function_call)
                call_id = _responses_call_id(function_call)
                args, parse_error = _responses_call_arguments(function_call)
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
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": model_payload.text,
                    }
                )
                stateless_input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": model_payload.text,
                    }
                )
                chat_messages.append(
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
            messages=list(chat_messages),
            tool_calls=list(tool_records),
            usage_records=list(usage_records),
            context_snapshots=list(context_snapshots),
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
                "free_team.max_output_tokens", None
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
        if self.provider_label == "kimi" and agent.model == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
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


def _responses_function_calls(output_items: Iterable[Any]) -> list[Any]:
    return [
        item for item in output_items if _responses_item_type(item) == "function_call"
    ]


def _serialize_responses_output_items(output_items: Iterable[Any]) -> list[Any]:
    """次ラウンドの input に引き継ぐため output item を素の dict へ変換する。"""
    serialized: list[Any] = []
    for item in output_items:
        if isinstance(item, dict):
            serialized.append(item)
            continue
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            try:
                serialized.append(dump(exclude_none=True))
                continue
            except Exception:  # noqa: BLE001
                pass
        serialized.append(item)
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


def _responses_call_to_chat_tool_call(function_call: Any) -> dict[str, Any]:
    """外部消費用の chat 形式 tool_call dict へ変換する。"""
    return {
        "id": _responses_call_id(function_call),
        "type": "function",
        "function": {
            "name": _responses_call_name(function_call),
            "arguments": _responses_call_raw_arguments(function_call),
        },
    }


def _normalize_tool_choice(value: Optional[str], *, has_tools: bool) -> Optional[str]:
    if not has_tools:
        return None
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "required", "none"}:
        return normalized
    return "auto"


def _assistant_message_payload(message: Any) -> dict[str, Any]:
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
    payload["role"] = str(payload.get("role") or getattr(message, "role", "assistant") or "assistant")
    payload["content"] = payload.get("content") or getattr(message, "content", "") or ""
    tool_calls = list(payload.get("tool_calls") or getattr(message, "tool_calls", None) or [])
    if tool_calls:
        payload["tool_calls"] = [
            dict(call) if isinstance(call, dict) else _serialize_tool_call(call)
            for call in tool_calls
        ]
    return payload


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
