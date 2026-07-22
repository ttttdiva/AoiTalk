"""無料Teamのターン固定・安全フォールバック対応LLM proxy。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Dict, Generator, Optional

from ..memory.history import HistoryManager
from ..services.free_team_service import (
    FreeTeamUnavailableError,
    acquire_route_lease,
    finalize_route_lease,
    free_team_profile,
    main_route_intent,
)
from .provider_capabilities import ProviderCapabilities


StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[list[str]]]

def disable_target_tools(target: Any) -> None:
    """tool-free候補でtoolが偶発実行されないよう空registryを固定する。"""

    from ..tools.registry import ToolRegistry

    empty_registry = ToolRegistry()
    setter = getattr(target, "set_tool_registry", None)
    if callable(setter):
        setter(empty_registry)
    if hasattr(target, "_native_tools_enabled"):
        target._native_tools_enabled = False
    if hasattr(target, "_tool_registry"):
        target._tool_registry = empty_registry
    recreate_agent = getattr(target, "_create_character_agent", None)
    if callable(recreate_agent) and hasattr(target, "agent"):
        target.agent = recreate_agent()


def _error_class(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    text = str(exc).lower()
    if status in {401, 403} or "unauthorized" in text or "authentication" in text:
        return "auth"
    if status == 402 or "payment required" in text or "credit" in text and "limit" in text:
        return "402"
    if status == 429 or "429" in text or "rate limit" in text or "quota" in text:
        return "429"
    if status and 500 <= int(status) < 600:
        return "5xx"
    if "timeout" in text or isinstance(exc, TimeoutError):
        return "timeout"
    if "connect" in text or "network" in text:
        return "connection"
    return "error"


def _usage_from_client(client: Any) -> dict[str, Any]:
    getter = getattr(client, "get_generation_metadata", None)
    if not callable(getter):
        return {}
    metadata = getter() or {}
    usage = metadata.get("cache_usage")
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key
        in {
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "requests",
            "usd",
        }
        and value is not None
    }


async def _call_target_method(
    target: Any,
    method_name: str,
    prompt: str,
    **kwargs: Any,
) -> Any:
    """候補clientが明示対応する引数だけを渡す。

    旧Gemini/CLI実装は共通stream callbackを持たない。未対応引数による
    TypeErrorを避け、tool副作用を観測できるかも同時に返す。
    """

    method = getattr(target, method_name, None)
    if not callable(method):
        method = getattr(target, "generate_response_async")
    parameters = inspect.signature(method).parameters
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    accepted = {
        key: value
        for key, value in kwargs.items()
        if accepts_kwargs or key in parameters
    }
    result = method(prompt, **accepted)
    if inspect.isawaitable(result):
        result = await result
    return result


def _target_supports_stream_callback(target: Any, method_name: str) -> bool:
    method = getattr(target, method_name, None)
    if not callable(method):
        method = getattr(target, "generate_response_async", None)
    if not callable(method):
        return False
    parameters = inspect.signature(method).parameters
    return "stream_callback" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


async def _cleanup_client(client: Any) -> None:
    cleanup = getattr(client, "cleanup", None)
    if not callable(cleanup):
        return
    result = cleanup()
    if inspect.isawaitable(result):
        await result


def _reservation_prompt(
    prompt: str,
    *,
    system_prompt: str = "",
    messages: list[dict[str, Any]] | None = None,
    summary: str = "",
    session_metadata: dict[str, Any] | None = None,
    tool_registry: Any = None,
) -> str:
    """実requestに含まれ得る入力を保守的に文字列化して予約量へ含める。"""

    parts: list[Any] = []
    if system_prompt:
        parts.append({"system": system_prompt})
    if summary:
        parts.append({"conversation_summary": summary})
    if messages:
        parts.append({"messages": messages})
    if session_metadata:
        parts.append({"session_context": session_metadata})
    if tool_registry is not None and hasattr(tool_registry, "get_all"):
        tools = []
        for tool in tool_registry.get_all():
            tools.append(
                {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", ""),
                    "parameters": tool.to_json_schema()
                    if hasattr(tool, "to_json_schema")
                    else {},
                }
            )
        if tools:
            parts.append({"tools": tools})
    parts.append({"user": prompt or ""})
    return json.dumps(parts, ensure_ascii=False, default=str)


class FreeTeamRoutingClient:
    """各ターンの開始前にRouteLeaseを取り、終了時に台帳を確定する。"""

    provider_label = "routing-profile"
    model_name = "free-team"

    def __init__(self, config: Any):
        self.config = config
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_response_format=True,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=False,
        )
        self.character_name = getattr(config, "default_character", "Assistant")
        self.history_manager = HistoryManager()
        self.memory_manager = None
        self.current_session_id: str | None = None
        self.current_project_id: str | None = None
        self.current_edit_message_id: str | None = None
        self.current_response_model: Any = None
        self.current_include_project_context: bool | None = None
        self.current_command_capabilities: tuple[str, ...] = ()
        self.current_tool_required: bool | None = None
        self.external_persistence_enabled = False
        self.session_user_id = "default_user"
        self.session_metadata: dict[str, Any] = {}
        self.generation_policy: Any = None
        self.system_prompt = ""
        self._active_client: Any = None
        self._last_client: Any = None
        self._last_route_metadata: dict[str, Any] = {}
        self._last_generation_metadata: dict[str, Any] = {}
        self._model_transcript: list[dict[str, Any]] = []
        self._last_model_transcript: list[dict[str, Any]] = []
        self._provider_state = {"previous_response_id": None, "fingerprint": None}
        self._provider_state_mode = "stateless"

    def set_session_context(self, user_id: str = "default_user", metadata: Any = None) -> None:
        self.session_user_id = user_id
        self.session_metadata = dict(metadata or {})

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = str(prompt or "")
        if self._active_client and hasattr(self._active_client, "set_system_prompt"):
            self._active_client.set_system_prompt(self.system_prompt)

    def set_character(self, character_name: str) -> None:
        self.character_name = character_name

    def update_character(self, character_name: str) -> None:
        self.set_character(character_name)

    def clear_history(self) -> None:
        self.history_manager.clear()
        self._model_transcript = []
        self._last_model_transcript = []
        self._provider_state = {"previous_response_id": None, "fingerprint": None}

    def set_llm_mode(self, mode: str) -> None:
        # effortは候補またはプール設定から決まり、通常モデルのmodeは転用しない。
        return None

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "free-team", "label": "無料Team"}]

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_label, "model": self.model_name}

    def _sync_to_target(self, target: Any) -> None:
        for field in (
            "current_session_id",
            "current_project_id",
            "current_edit_message_id",
            "current_response_model",
            "current_include_project_context",
            "current_command_capabilities",
            "current_tool_required",
            "external_persistence_enabled",
            "session_user_id",
            "session_metadata",
            "generation_policy",
            "character_name",
            "system_prompt",
        ):
            if hasattr(target, field):
                setattr(target, field, getattr(self, field))
        if hasattr(target, "history_manager"):
            target.history_manager = self.history_manager
        if hasattr(target, "_model_transcript"):
            target._model_transcript = [dict(item) for item in self._model_transcript]
        if self.system_prompt and hasattr(target, "set_system_prompt"):
            target.set_system_prompt(self.system_prompt)

    def _sync_from_target(self, target: Any) -> None:
        if hasattr(target, "history_manager"):
            self.history_manager = target.history_manager
        self._model_transcript = [
            dict(item) for item in getattr(target, "_model_transcript", [])
        ]
        self._last_model_transcript = [
            dict(item) for item in getattr(target, "_last_model_transcript", [])
        ]

    async def _execute(
        self,
        method_name: str,
        prompt: str,
        *,
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
        system_prompt: str | None = None,
    ) -> str:
        from .manager import create_llm_client_for_target

        intent = main_route_intent(self.config)
        if intent.kind != "pool":
            raise FreeTeamUnavailableError("無料Teamのルーティング設定が無効です")
        required = {"text"}
        if image_data:
            required.add("vision")
        tool_registry = None
        tools_enabled = method_name == "generate_response_async" and bool(
            self.config.get("use_tools", True)
        )
        if tools_enabled:
            try:
                from .runtime_tool_registry import build_runtime_tool_registry

                tool_registry = build_runtime_tool_registry(self.config)
            except Exception:
                tool_registry = None
        # 自然文keywordでは判定せず、poolの明示tool intentを優先する。
        tool_mode = str(intent.tool_mode or "auto").lower()
        explicit_tool_required = getattr(self, "current_tool_required", None)
        tools_required = (
            bool(explicit_tool_required)
            if isinstance(explicit_tool_required, bool)
            else tools_enabled and tool_mode != "disabled"
        )
        if tools_required:
            required.add("tools")
        profile = free_team_profile(self.config)
        max_fallbacks = max(0, min(10, int(profile.get("max_fallbacks") or 0)))
        excluded: set[str] = set()
        last_error: BaseException | None = None

        model_messages = self.history_manager.get_model_messages()
        if self._model_transcript:
            model_messages = [dict(item) for item in self._model_transcript]
        reservation_prompt = _reservation_prompt(
            prompt,
            system_prompt=system_prompt or self.system_prompt,
            messages=model_messages,
            summary=self.history_manager.summary,
            session_metadata=self.session_metadata,
            tool_registry=tool_registry if tools_required else None,
        )

        for fallback_count in range(max_fallbacks + 1):
            history_snapshot = {
                "history": [dict(item) for item in self.history_manager.history],
                "model_history": (
                    None
                    if self.history_manager.model_history is None
                    else [dict(item) for item in self.history_manager.model_history]
                ),
                "summary": self.history_manager.summary,
                "summary_version": self.history_manager.summary_version,
                "summary_checkpoint": self.history_manager.summary_checkpoint,
                "model_transcript": [dict(item) for item in self._model_transcript],
                "last_model_transcript": [
                    dict(item) for item in self._last_model_transcript
                ],
            }
            lease = await acquire_route_lease(
                intent,
                prompt=reservation_prompt,
                required_capabilities=required,
                excluded_candidate_ids=excluded,
                fallback_count=fallback_count,
            )
            excluded.add(lease.candidate_id)
            target: Any = None
            side_effect_started = False
            side_effect_observable = False
            provider_request_started = False

            async def monitored_callback(event_type: str, data: Dict[str, Any]) -> None:
                nonlocal side_effect_started
                normalized = str(event_type or "").lower()
                if "tool" in normalized and (
                    "start" in normalized or "call" in normalized or "execut" in normalized
                ):
                    side_effect_started = True
                if stream_callback:
                    await stream_callback(event_type, data)

            started = time.perf_counter()
            try:
                self._last_route_metadata = lease.safe_metadata()
                await self._publish_route_metadata(lease)
                target = create_llm_client_for_target(
                    self.config,
                    provider=lease.provider,
                    model=lease.model,
                    effort=lease.effort,
                    base_url=lease.base_url,
                    api_key=lease.api_key,
                    provider_options={
                        **lease.provider_options,
                        "max_output_tokens": lease.max_output_tokens,
                    },
                )
                self._sync_to_target(target)
                if not tools_required:
                    disable_target_tools(target)
                self._active_client = target
                side_effect_observable = _target_supports_stream_callback(
                    target, method_name
                )
                kwargs: dict[str, Any] = {}
                if method_name == "generate_response_async":
                    kwargs = {
                        "max_tokens": lease.max_output_tokens,
                        "image_data": image_data,
                        "stream_callback": monitored_callback,
                        "steering_callback": steering_callback,
                    }
                elif method_name in {"generate_plain_text_async", "generate_memory_extraction_async"}:
                    kwargs = {
                        "system_prompt": system_prompt or self.system_prompt,
                        "max_tokens": lease.max_output_tokens,
                    }
                provider_request_started = True
                response = await asyncio.wait_for(
                    _call_target_method(target, method_name, prompt, **kwargs),
                    timeout=max(1, lease.timeout_seconds),
                )
                latency_ms = (time.perf_counter() - started) * 1000
                usage = _usage_from_client(target)
                await finalize_route_lease(
                    lease,
                    actual_usage=usage,
                    success=True,
                    latency_ms=latency_ms,
                )
                self._sync_from_target(target)
                target_metadata = (
                    target.get_generation_metadata()
                    if hasattr(target, "get_generation_metadata")
                    else {}
                )
                self._last_route_metadata = lease.safe_metadata()
                self._last_generation_metadata = {
                    **dict(target_metadata or {}),
                    "free_team_route": self._last_route_metadata,
                }
                previous_client = self._last_client
                self._last_client = target
                if previous_client is not None and previous_client is not target:
                    await _cleanup_client(previous_client)
                return str(response)
            except asyncio.CancelledError:
                # thread/external CLIはcancel後も継続し得るため、最大予約を消費し
                # fallbackせず、shieldして台帳だけは必ず閉じる。
                await asyncio.shield(
                    finalize_route_lease(
                        lease,
                        success=False,
                        consume_reserved_on_failure=provider_request_started,
                        error_class="cancelled",
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                )
                raise
            except Exception as exc:
                last_error = exc
                error_class = _error_class(exc)
                reported_side_effect = getattr(
                    exc, "free_team_side_effect_started", None
                )
                if reported_side_effect is not None:
                    side_effect_started = bool(reported_side_effect)
                    side_effect_observable = True
                await finalize_route_lease(
                    lease,
                    success=False,
                    consume_reserved_on_failure=error_class == "timeout",
                    error_class=error_class,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                retryable = error_class in {"429", "402", "5xx", "timeout", "connection"}
                tools_may_have_run_unobserved = (
                    "tools" in required and not side_effect_observable
                )
                if (
                    side_effect_started
                    or tools_may_have_run_unobserved
                    or error_class == "timeout"
                    or not retryable
                    or fallback_count >= max_fallbacks
                ):
                    raise
                self.history_manager.history = history_snapshot["history"]
                self.history_manager.model_history = history_snapshot["model_history"]
                self.history_manager.summary = history_snapshot["summary"]
                self.history_manager.summary_version = history_snapshot["summary_version"]
                self.history_manager.summary_checkpoint = history_snapshot[
                    "summary_checkpoint"
                ]
                self._model_transcript = history_snapshot["model_transcript"]
                self._last_model_transcript = history_snapshot[
                    "last_model_transcript"
                ]
            finally:
                self._active_client = None
                if target is not None and target is not self._last_client:
                    await _cleanup_client(target)

        if last_error:
            raise last_error
        raise FreeTeamUnavailableError("無料Teamの利用可能枠がありません")

    async def _publish_route_metadata(self, lease: Any) -> None:
        """実候補をroot Agent Runへ安全なmetadataとして反映する。"""

        try:
            from ..services.agent_run_service import (
                AgentRunService,
                get_current_agent_run_id,
            )

            run_id = get_current_agent_run_id()
            if run_id:
                route_metadata = lease.safe_metadata()
                await AgentRunService().mark_running(
                    run_id,
                    metadata={"free_team_route": route_metadata},
                    provider=lease.provider,
                    model=lease.model,
                )
                await AgentRunService().record_event(
                    run_id,
                    "agent_team.route_selected",
                    status="running",
                    message="無料Teamの実行候補を選択しました",
                    payload=route_metadata,
                )
        except Exception:
            # Timeline記録失敗で生成結果を失敗扱いにしない。
            return

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        return await self._execute(
            "generate_response_async",
            user_input,
            image_data=image_data,
            stream_callback=stream_callback,
            steering_callback=steering_callback,
        )

    async def generate_plain_text_async(
        self, prompt: str, *, system_prompt: Optional[str] = None
    ) -> str:
        return await self._execute(
            "generate_plain_text_async", prompt, system_prompt=system_prompt
        )

    async def generate_memory_extraction_async(
        self, prompt: str, *, system_prompt: str
    ) -> str:
        return await self._execute(
            "generate_memory_extraction_async", prompt, system_prompt=system_prompt
        )

    async def generate_async(self, prompt: str) -> str:
        return await self.generate_response_async(prompt)

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: dict | None = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> str | Generator[str, None, None]:
        def run() -> str:
            return asyncio.run(
                self.generate_response_async(
                    user_input,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    image_data=image_data,
                    stream_callback=stream_callback,
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run).result()
        if stream:
            return iter((result,))
        return result

    def generate(self, prompt: str) -> str:
        return str(self.generate_response(prompt, stream=False))

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        prompt = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}" for item in messages
        )
        return self.generate(prompt)

    def stream_chat(self, messages: list[dict[str, Any]], **kwargs: Any):
        yield self.chat(messages, **kwargs)

    def get_generation_metadata(self) -> dict[str, Any]:
        return dict(self._last_generation_metadata)

    def _get_memory_metadata(self) -> dict[str, Any]:
        return {"free_team_route": dict(self._last_route_metadata)}

    async def cleanup(self) -> None:
        clients = [self._active_client, self._last_client]
        seen: set[int] = set()
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            await _cleanup_client(client)
