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
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall
from .generation_error import GenerationFailure, empty_response_failure
from ..services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
)

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


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

    async def run(
        self,
        agent: AgentDefinition,
        user_input: str | list[dict[str, Any]],
        *,
        stream_callback: Optional[StreamCallback] = None,
        tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None = None,
    ) -> NativeRunResult:
        """Run one turn, optionally resolving tools before each model round.

        ``load_tool_pack`` changes the effective tool set during a turn.  A
        caller that owns the session (the normal ``AgentLLMClient`` path)
        can provide a resolver so the next model request receives the newly
        loaded function schemas.  Direct callers retain the historical static
        ``agent.tools`` behavior when no resolver is supplied.
        """
        # AgentTurnRunner is created before the per-turn AgentDefinition is
        # assembled.  Resolve again here so the official model registry sees
        # the model that will actually be sent over the wire.
        self._refresh_context_budget(agent.model)
        return await self._run_core(
            agent,
            user_input,
            stream_callback=stream_callback,
            tools_provider=tools_provider,
        )

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

        seed_messages = _prompt_messages_or_user(user_input)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.instructions or ""},
            *seed_messages,
        ]
        plain_user_input = prompt_text(user_input)
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

        for round_index in range(self.max_tool_rounds + 1):
            # A tool call can load a deferred pack.  Resolve the effective
            # registry and schemas again before the next provider request so
            # the newly loaded tools are callable as native functions.
            if round_index:
                active_tools = _resolve_runtime_tools(agent, tools_provider)
                tools_payload = _tool_specs(active_tools)
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
            protected = await self.privacy_gateway.protect(
                kwargs,
                provider=self.provider_label,
                base_url=str(getattr(self.client, "base_url", "") or ""),
                source_kind="model_request",
            )
            kwargs = protected.payload
            response = await self.client.chat.completions.create(**kwargs)
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
            assistant_payload = _assistant_message_payload(message)

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
        active_tools = _resolve_runtime_tools(agent, tools_provider)
        tools_payload = _responses_tool_specs(active_tools)
        requested_tool_choice = _normalize_tool_choice(
            agent.model_settings.tool_choice,
            has_tools=bool(tools_payload),
        )
        current_tool_choice = requested_tool_choice
        effort = getattr(agent.model_settings.reasoning, "effort", None)
        final_output = ""

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
            protected = await self.privacy_gateway.protect(
                request_kwargs,
                provider=self.provider_label,
                base_url=str(getattr(self.client, "base_url", "") or ""),
                source_kind="model_request",
            )
            request_kwargs = protected.payload
            try:
                return await self.client.responses.create(**request_kwargs)
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
                protected_retry = await self.privacy_gateway.protect(
                    retry_kwargs,
                    provider=self.provider_label,
                    base_url=str(getattr(self.client, "base_url", "") or ""),
                    source_kind="model_request",
                )
                return await self.client.responses.create(**protected_retry.payload)

        for round_index in range(self.max_tool_rounds + 1):
            # See the chat-completions path above.  Keep the provider payload
            # and execution registry in lockstep with the session's current
            # deferred-pack state on every round.
            if round_index:
                active_tools = _resolve_runtime_tools(agent, tools_provider)
                tools_payload = _responses_tool_specs(active_tools)
            tool_registry = ToolRegistry()
            for tool in active_tools:
                tool_registry.register(tool)
            tool_router = RegistryToolRouter(
                tool_registry,
                log_prefix=f"NativeAgentTurnRunner:{agent.name}",
                config=self.config,
                user_input=plain_user_input,
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

            try:
                response = await create_response(kwargs)
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
                request_snapshot = safe_build_request_snapshot(
                    retry_kwargs,
                    request_kind="responses.stateless_retry",
                    include_provider_context=False,
                )
                if request_snapshot is not None:
                    context_snapshots.append(request_snapshot)
                response = await create_response(retry_kwargs)
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
            if content:
                # function_call を伴うラウンドの通常テキストは途中経過として配信する。
                # 最終ラウンドは stream_token 側で配信するためここでは発行しない。
                await _emit(
                    stream_callback,
                    "assistant_text",
                    {"text": content, "round": round_index},
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
    privacy_gateway: OutboundPrivacyGateway | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
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


def _resolve_runtime_tools(
    agent: AgentDefinition,
    tools_provider: Callable[[AgentDefinition], Iterable[ToolDefinition]] | None,
) -> list[ToolDefinition]:
    """Resolve the native tools for one model round.

    Tool-pack/session resolvers are intentionally best-effort: a resolver
    failure must not make the model turn disappear, and the static agent tool
    list remains the safe fallback for legacy/direct callers.
    """
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
