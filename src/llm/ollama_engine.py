"""Ollama local LLM client using Ollama's OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from typing import Any, Dict, Generator, List, Optional, Union

from openai import OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..services.project_context import (
    format_project_context_for_chat_prompt,
    format_minimal_project_context_for_chat_prompt,
    ProjectContextResolver,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..services.story_chat_context import run_story_chat_context_sync
from ..services.outbound_privacy_service import OutboundPrivacyGateway
from ..tools.adapters import OpenAIAPIAdapter
from ..tools.registry import ToolRegistry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_cancellation import GenerationInterrupted
from .agentic_completion import (
    agentic_max_rounds,
    render_messages_for_review,
    run_agentic_completion_loop_sync,
)
from .json_tool_loop import build_json_tool_loop_system_prompt, run_json_tool_loop
from .agent_runtime import (
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
    run_openai_tool_call_loop,
)
from .prompts import build_unified_instructions
from .provider_capabilities import ProviderCapabilities
from .multimodal import openai_content_parts
from .provider_mode_adapters import (
    ollama_mode_options_for_model,
    ollama_reasoning_effort_for_mode,
)
from .conversation_context import (
    build_prompt_messages,
    normalize_usage,
    persist_usage_sync,
    stable_cache_key,
)
from .openai_compatible_local_profiles import openai_compatible_server_profile
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import ensure_load_tool_pack_tool
from .turn_stream_events import (
    SyncStreamEmitter,
    bind_stream_callback_loop,
    emit_thinking,
    make_sync_stream_emitter,
    strip_leading_think_markup,
    thinking_text_from_message,
)
from .context_snapshot import (
    openai_compatible_request_components,
    reconcile_snapshot,
    sanitized_snapshot_series,
    snapshot,
)
from ..services.context_builder import _needs_detailed_project_context
from .tool_exposure import filtered_registry_for_client
from .tool_policy import (
    project_progress_review_active,
    reset_current_user_input,
    set_current_user_input,
)

logger = logging.getLogger(__name__)


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_OLLAMA_MODEL = "gemma4:e4b"
DEFAULT_OLLAMA_API_KEY = "ollama"


def _config_get(config: Optional[Config], key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _normalize_base_url(base_url: str) -> str:
    clean = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    if clean.endswith("/v1"):
        return clean
    return f"{clean}/v1"


class OllamaClient:
    """OpenAI-compatible client for an already running Ollama server."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        api_key: str = DEFAULT_OLLAMA_API_KEY,
        config: Optional[Config] = None,
    ):
        self.config = config
        self._privacy_gateway = OutboundPrivacyGateway(config)
        self.base_url = _normalize_base_url(base_url)
        self.model_name = model or DEFAULT_OLLAMA_MODEL
        self.api_key = api_key or DEFAULT_OLLAMA_API_KEY
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self.server_profile = openai_compatible_server_profile(
            config, base_url=self.base_url, provider="ollama"
        )
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_response_format=True,
            supports_model_pull=True,
            supports_model_delete=True,
            supports_extra_body=False,
        )

        if hasattr(config, "default_character"):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get("default_character", "Assistant")
        else:
            self.character_name = "Assistant"

        self.history_manager = HistoryManager()
        self._native_tool_calling_enabled = bool(
            _config_get(config, "ollama.enable_tools", False)
        )
        if config and _config_get(config, "use_tools", True):
            self._tool_registry = build_runtime_tool_registry_for_client(
                build_runtime_tool_registry,
                config,
                client=self,
            )
            ensure_load_tool_pack_tool(self._tool_registry, self)
        else:
            self._tool_registry = ToolRegistry()

        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_llm_mode = "fast"
        self._last_model_transcript: list[dict[str, Any]] = []
        self._last_usage: dict[str, Any] = {}
        self._last_tool_loop_messages: list[dict[str, Any]] = []
        self._last_context_snapshots: list[dict[str, Any]] = []
        self._current_dynamic_context: list[tuple[str, str]] = []
        self._current_dynamic_context_metadata: dict[str, dict[str, Any]] = {}
        self.keep_alive = _config_get(config, "ollama.keep_alive", None)
        self.cache_prompt = bool(_config_get(config, "ollama.cache_prompt", True))

        self.system_prompt = self._build_system_prompt()

        logger.info("[OllamaClient] initialized")
        logger.info("[OllamaClient] Base URL: %s", self.base_url)
        logger.info("[OllamaClient] Model: %s", self.model_name)
        logger.info("[OllamaClient] Character: %s", self.character_name)

    def _get_session_user_id(self) -> str:
        if self.session_user_id:
            return self.session_user_id
        metadata_user_id = self.session_metadata.get("user_id")
        return str(metadata_user_id) if metadata_user_id else "default_user"

    def _sync_privacy_gateway(self) -> OutboundPrivacyGateway:
        user_id = str(self._get_session_user_id() or "default_user")
        session_id = str(getattr(self, "current_session_id", None) or "")
        if (
            self._privacy_gateway.user_id != user_id
            or self._privacy_gateway.session_id != session_id
        ):
            self._privacy_gateway = OutboundPrivacyGateway(
                self.config,
                user_id=user_id,
                session_id=session_id,
                session_context=self._privacy_session_context,
                project_metadata=self._privacy_project_metadata,
            )
        else:
            if self._privacy_session_context or self._privacy_project_metadata:
                self._privacy_gateway.update_policy_context(
                    session_context=self._privacy_session_context,
                    project_metadata=self._privacy_project_metadata,
                )
            else:
                self._privacy_gateway.update_policy_context()
        return self._privacy_gateway

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata = {
            "model_transcript": [dict(item) for item in self._last_model_transcript],
            "cache_usage": dict(self._last_usage),
            "cache_diagnostics": {
                "provider": "ollama",
                "model": self.model_name,
                "cache_provider": "ollama",
                "cache_mode": "cache_prompt" if self.cache_prompt else "disabled",
                "cache_supported": True,
                "cache_active": self._last_usage.get("cache_active"),
                "cache_configured": self.cache_prompt,
                "keep_alive": self.keep_alive,
                "metrics_source": "ollama_openai_compatible_response",
                "cache_key": getattr(self, "_cache_key", None),
            },
        }
        if self._last_context_snapshots:
            bounded = sanitized_snapshot_series(self._last_context_snapshots)
            if bounded:
                metadata["context_snapshot"] = bounded
        return metadata

    def _record_context_snapshot(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_kind: str = "chat.completions",
    ) -> None:
        try:
            values = list(getattr(self, "_last_context_snapshots", []) or [])
            values.append(
                snapshot(
                    provider="ollama",
                    model=str(api_kwargs.get("model") or self.model_name),
                    components=openai_compatible_request_components(
                        api_kwargs.get("messages") or [],
                        api_kwargs.get("tools") or [],
                        provider="ollama",
                        dynamic_context=getattr(
                        self, "_current_dynamic_context", []
                    ),
                    dynamic_context_metadata=getattr(
                        self, "_current_dynamic_context_metadata", {}
                    ),
                    ),
                    request_index=len(values),
                    request_kind=request_kind,
                )
            )
            self._last_context_snapshots = values[-32:]
        except Exception:
            logger.warning(
                "[OllamaClient] context observation failed; continuing",
                exc_info=True,
            )

    def _capture_usage(self, response: Any) -> None:
        raw = getattr(response, "usage", None)
        resolved_model = getattr(response, "model", None)
        if raw is None and isinstance(response, dict):
            raw = response.get("usage")
        if resolved_model is None and isinstance(response, dict):
            resolved_model = response.get("model")
        self._last_usage = {
            key: value
            for key, value in normalize_usage(
                raw,
                provider="ollama",
                resolved_model=(str(resolved_model) if resolved_model else None),
            ).items()
            if value is not None
        }
        input_tokens = self._last_usage.get("input_tokens")
        if input_tokens is not None and self._last_context_snapshots:
            self._last_context_snapshots[-1] = reconcile_snapshot(
                self._last_context_snapshots[-1],
                int(input_tokens),
            )

    def _capture_and_persist_usage(
        self,
        response: Any,
        *,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> dict[str, Any]:
        """Capture and persist one successful Ollama API response."""

        previous_usage = dict(getattr(self, "_last_usage", {}) or {})
        self._capture_usage(response)
        usage = dict(self._last_usage)
        if not usage and previous_usage:
            # Ephemeral/test endpoints often omit usage entirely.  Preserve
            # the last confirmed turn observation rather than erasing it.
            self._last_usage = previous_usage
        if usage:
            persist_usage_sync(
                self,
                provider="ollama",
                model=self.model_name,
                usage=usage,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )
        return usage

    def _record_model_transcript(self, messages: List[Dict[str, Any]], response_text: str) -> None:
        source_messages = self._last_tool_loop_messages or messages
        self._last_model_transcript = [
            dict(message)
            for message in source_messages
            if message.get("role") in {"user", "assistant", "tool"}
        ]
        if response_text:
            self._last_model_transcript.append({"role": "assistant", "content": response_text})
        if callable(getattr(self.history_manager, "set_model_messages", None)):
            self.history_manager.set_model_messages(self._last_model_transcript)

    def _build_system_prompt(self) -> str:
        if not self.config:
            return "あなたは親切なAIアシスタントです。"

        try:
            custom_instructions = get_user_custom_instructions_sync(
                self._get_session_user_id()
            )
        except Exception as exc:
            logger.debug("[OllamaClient] Failed to load custom instructions: %s", exc)
            custom_instructions = ""

        try:
            return build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                include_static_tool_reference=False,
                custom_instructions=custom_instructions,
                # Ollama's enabled path uses native function tools; its
                # disabled path uses the JSON loop/plain completion.  Neither
                # path consumes the legacy textual ``[TOOL_CALL]`` syntax.
                tool_protocol="native",
            )
        except Exception as exc:
            logger.warning("[OllamaClient] Falling back to basic prompt: %s", exc)
            return f"あなたは{self.character_name}です。"

    def set_character(self, character_name: str) -> None:
        self.character_name = character_name
        self.system_prompt = self._build_system_prompt()
        logger.info("[OllamaClient] Character changed: %s", character_name)

    def update_character(self, yaml_filename: str) -> None:
        if not self.config:
            return
        character_config = self.config.get_character_config(yaml_filename)
        self.character_name = character_config.get("name", yaml_filename)
        self.clear_history()
        self.system_prompt = self._build_system_prompt()
        logger.info("[OllamaClient] Character updated: %s", self.character_name)

    def set_system_prompt(self, prompt: str) -> None:
        if self.config:
            self.system_prompt = self._build_system_prompt()
            return
        self.system_prompt = prompt

    def set_llm_mode(self, mode: str) -> None:
        options = set(ollama_mode_options_for_model(self.model_name))
        if mode not in options:
            invalid_mode = mode
            mode = "medium" if "medium" in options else "fast"
            logger.warning(
                "[OllamaClient] Invalid mode '%s', using %s",
                invalid_mode,
                mode,
            )
        self._current_llm_mode = mode

    def get_llm_mode(self) -> str:
        return self._current_llm_mode

    def set_session_context(
        self,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )
        self.system_prompt = self._build_system_prompt()

    def _build_messages(
        self,
        user_input: str,
        *,
        dynamic_context: Optional[list[tuple[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        context_window = getattr(self.history_manager, "context_window_size", 10)
        history = self._history_messages_for_prompt()[-(context_window * 2) :]
        return [
            {"role": "system", "content": getattr(self, "system_prompt", "")},
            *build_prompt_messages(
                history,
                summary=getattr(self.history_manager, "summary", ""),
                current_user_input=user_input,
                dynamic_context=dynamic_context or [],
            ),
        ]

    def _history_messages_for_prompt(self) -> list[dict[str, Any]]:
        get_model_messages = getattr(self.history_manager, "get_model_messages", None)
        if callable(get_model_messages):
            return [dict(message) for message in get_model_messages()]
        get_all = getattr(self.history_manager, "get_all", None)
        if callable(get_all):
            return [dict(message) for message in get_all()]
        legacy_messages = getattr(self.history_manager, "messages", [])
        return [
            {"role": item[0], "content": item[1]}
            for item in legacy_messages
            if isinstance(item, (tuple, list)) and len(item) >= 2
        ]

    def _build_json_tool_user_message(self, user_input: str) -> str:
        return "\n".join(
            [
                "User request:",
                user_input,
                "",
                "Decide whether a tool is needed.",
                (
                    "Choose a tool only when the request requires external, current, or "
                    "otherwise tool-backed information; do not infer a tool from a single keyword."
                ),
                (
                    "When calling a tool with a `request` parameter, copy the user's request exactly "
                    "unless a shorter accurate query is obvious."
                ),
            ]
        )

    def _build_json_tool_loop_messages(self, user_input: str) -> List[Dict[str, str]]:
        registry = filtered_registry_for_client(self, self._tool_registry)
        system_prompt = build_json_tool_loop_system_prompt(
            getattr(self, "system_prompt", ""),
            registry,
        )
        messages = [{"role": "system", "content": system_prompt}]
        context_window = getattr(self.history_manager, "context_window_size", 10)
        history = self._history_messages_for_prompt()
        tool_hint_context = self._build_tool_hint_context(user_input)
        return [
            *messages,
            *build_prompt_messages(
                history[-(context_window * 2) :],
                summary=getattr(self.history_manager, "summary", ""),
                current_user_input=self._build_json_tool_user_message(user_input),
                dynamic_context=(
                    [("Current tool hints", tool_hint_context)]
                    if tool_hint_context
                    else []
                ),
            ),
        ]

    def _build_api_kwargs(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        tools_enabled: bool = True,
    ) -> Dict[str, Any]:
        api_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 1024,
        }
        reasoning_effort = ollama_reasoning_effort_for_mode(
            self.model_name,
            self._current_llm_mode,
        )
        if reasoning_effort:
            api_kwargs["reasoning_effort"] = reasoning_effort

        if tools_enabled and len(self._tool_registry) > 0:
            registry = filtered_registry_for_client(self, self._tool_registry)
            if len(registry) > 0:
                api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(
                    registry.get_all()
                )
                api_kwargs["tool_choice"] = "auto"

        self._cache_key = stable_cache_key(
            user_id=self._get_session_user_id(),
            session_id=getattr(self, "current_session_id", None),
            project_id=self.current_project_id,
            character=self.character_name,
            model=self.model_name,
            system_prompt=self.system_prompt,
            tool_schemas=api_kwargs.get("tools", []),
            provider="ollama",
            branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
            summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
            server_instance=str(self.session_metadata.get("server_instance") or "default-instance"),
        )

        return api_kwargs

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        self._last_context_snapshots = []
        self._current_dynamic_context = []
        self._current_dynamic_context_metadata = {}
        api_kwargs = self._build_api_kwargs(messages, temperature, max_tokens)
        response = self._create_completion_with_tool_fallback(api_kwargs)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(
                messages=messages,
                assistant_message=choice.message,
                api_kwargs=api_kwargs,
                registry=filtered_registry_for_client(self, self._tool_registry),
            )
        return self._privacy_gateway.restore(choice.message.content or "")

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
        request_type: str = "memory_extraction",
    ) -> str:
        """Run side-effect-free extraction without touching turn/history state."""
        def create_extraction() -> Any:
            started_at = time.monotonic()
            self._sync_privacy_gateway()
            protected = self._privacy_gateway.protect_sync(
                {
                    "model": self.model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 1200,
                },
                provider="ollama",
                base_url=self.base_url,
                source_kind=request_type,
            )
            response = self.client.chat.completions.create(
                **protected.payload,
            )
            self._capture_and_persist_usage(
                response,
                request_type=request_type,
                latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            )
            return response

        response = await asyncio.to_thread(create_extraction)
        return self._privacy_gateway.restore(
            str(response.choices[0].message.content or "")
        )

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """履歴・ツールを変更せずプレーンテキストを生成する。"""
        return await self.generate_memory_extraction_async(
            prompt,
            system_prompt=(
                system_prompt
                or "You are a concise assistant. Do not call tools or modify files."
            ),
            request_type="plain",
        )

    async def generate_title_async(self, prompt: str) -> str:
        """Generate a side-effect-free Ollama title and meter it separately."""

        return await self.generate_memory_extraction_async(
            prompt,
            system_prompt="You generate concise titles. Return only the title.",
            request_type="title",
        )

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        self._sync_privacy_gateway()
        self._last_context_snapshots = []
        self._current_dynamic_context = []
        self._current_dynamic_context_metadata = {}
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            tools_enabled=False,
        )
        api_kwargs["stream"] = True
        api_kwargs = self._privacy_gateway.protect_sync(
            api_kwargs,
            provider="ollama",
            base_url=self.base_url,
            source_kind="model_request",
        ).payload
        self._record_context_snapshot(api_kwargs, request_kind="chat.completions.stream")
        stream = self.client.chat.completions.create(**api_kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield self._privacy_gateway.restore_aliases(
                    chunk.choices[0].delta.content
                )

    def list_models(self) -> List[Dict[str, Any]]:
        response = self.client.models.list()
        data = getattr(response, "data", response)
        models = []
        for item in data:
            model_id = getattr(item, "id", None)
            if model_id is None and isinstance(item, dict):
                model_id = item.get("id")
            if model_id:
                models.append({"id": model_id})
        return models

    def health_check(self) -> Dict[str, Any]:
        try:
            models = self.list_models()
            return {"ok": True, "base_url": self.base_url, "model_count": len(models)}
        except Exception as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc)}

    def _run_async_sync(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _get_story_chat_context_sync(self):
        """Resolve the trusted StoryWritingSession for this conversation."""
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(
            self._run_async_sync,
            str(self.current_session_id),
        )

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        current_project_id = getattr(self, "current_project_id", None)
        current_session_id = getattr(self, "current_session_id", None)
        if not current_project_id and not current_session_id:
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_sync(
                resolver.resolve_context(
                    project_id=current_project_id,
                    session_id=current_session_id,
                    user_id=self._get_session_user_id(),
                )
            )
        except Exception as exc:
            logger.warning("[OllamaClient] Failed to resolve project context: %s", exc)
            return None

    def _build_tool_hint_context(self, user_input: str) -> str:
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=filtered_registry_for_client(self, self._tool_registry),
            policy=get_client_generation_policy(self),
            log_prefix="OllamaClient",
        )

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> Union[str, Generator[str, None, None]]:
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        policy = get_client_generation_policy(self)
        generation_policy_token = set_current_generation_policy(policy)
        try:
            self._last_tool_loop_messages = []
            self._last_context_snapshots = []
            self._current_dynamic_context_metadata = {}
            project_context_started = time.perf_counter()
            project_context = self._resolve_project_context_sync()
            project_context_duration_ms = (
                time.perf_counter() - project_context_started
            ) * 1000
            project_token = set_runtime_project_context(project_context)
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            self._sync_privacy_gateway()
            tool_hint_started = time.perf_counter()
            tool_hint_context = self._build_tool_hint_context(
                user_input
            )
            tool_hint_duration_ms = (
                time.perf_counter() - tool_hint_started
            ) * 1000
            dynamic_context: list[tuple[str, str]] = []
            model_project_context = (
                project_context if project_context_enabled_for_client(self) else None
            )
            if model_project_context:
                dynamic_context.append(
                    (
                        "Current Project Context",
                        (
                            format_project_context_for_chat_prompt(model_project_context)
                            if _needs_detailed_project_context(user_input)
                            else format_minimal_project_context_for_chat_prompt(
                                model_project_context
                            )
                        ),
                    )
                )
            if tool_hint_context:
                dynamic_context.append(("Current tool hints", tool_hint_context))
            self._current_dynamic_context = list(dynamic_context)
            self._current_dynamic_context_metadata = {
                "Current Project Context": {
                    "duration_ms": project_context_duration_ms
                },
                "Current tool hints": {
                    "duration_ms": tool_hint_duration_ms
                },
            }

            if (
                policy.discretionary_tool_loop_enabled
                and not stream
                and len(self._tool_registry) > 0
                and not self._native_tool_calling_enabled
            ):
                response_text = self._generate_with_json_tool_loop(
                    user_input,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    original_request=user_input,
                )
                response_text = run_agentic_completion_loop_sync(
                    client=self,
                    run_once=lambda prompt: self._run_agentic_review_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        original_request=user_input,
                    ),
                    context=render_messages_for_review(
                        self._build_messages(user_input, dynamic_context=dynamic_context)
                    ),
                    user_input=user_input,
                    initial_response=response_text,
                )
                self.history_manager.add_message("user", user_input)
                self.history_manager.add_message("assistant", response_text)
                return response_text

            messages = self._build_messages(
                user_input,
                dynamic_context=dynamic_context,
            )
            if image_data and messages:
                messages[-1]["content"] = openai_content_parts(
                    str(messages[-1].get("content") or ""),
                    image_data,
                )
            api_kwargs = self._build_api_kwargs(
                messages,
                temperature,
                max_tokens,
                tools_enabled=self._native_tool_calling_enabled,
            )

            if stream:
                return self._stream_response(api_kwargs, user_input)

            # Keep the historical one-argument call shape for test doubles and
            # alternate adapters; the helper's default records it as chat.
            response = self._create_completion_with_tool_fallback(api_kwargs)
            choice = response.choices[0]
            turn_event_emitter = make_sync_stream_emitter(stream_callback)
            if getattr(choice.message, "tool_calls", None):
                response_text = self._handle_tool_calls(
                    messages=messages,
                    assistant_message=choice.message,
                    api_kwargs=api_kwargs,
                    registry=filtered_registry_for_client(
                        self,
                        self._tool_registry,
                    ),
                    user_input=user_input,
                    event_callback=turn_event_emitter,
                )
            elif project_progress_review_active(user_input):
                response_text = self._handle_tool_calls(
                    messages=messages,
                    assistant_message=choice.message,
                    api_kwargs=api_kwargs,
                    registry=filtered_registry_for_client(
                        self,
                        self._tool_registry,
                    ),
                    user_input=user_input,
                    event_callback=turn_event_emitter,
                )
            else:
                # ツール往復が無い場合でも reasoning があれば thinking として配信する。
                emitted_thinking = emit_thinking(
                    turn_event_emitter,
                    thinking_text_from_message(choice.message),
                    round_index=0,
                )
                message_content = choice.message.content or ""
                if emitted_thinking:
                    # thinking として配信済みの <think> を本文へ二重表示しない。
                    message_content = strip_leading_think_markup(message_content)
                response_text = guard_tool_execution_claims(
                    message_content,
                    [],
                )

            response_text = run_agentic_completion_loop_sync(
                client=self,
                run_once=lambda prompt: self._run_agentic_review_once(
                    prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    original_request=user_input,
                ),
                context=render_messages_for_review(messages),
                user_input=user_input,
                initial_response=response_text,
            )
            self._record_model_transcript(messages, response_text)
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response_text)
            return response_text
        except GenerationInterrupted:
            raise
        except Exception as exc:
            logger.error("[OllamaClient] generation failed: %s", exc, exc_info=True)
            fallback = self._get_fallback_response()
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", fallback)
            if stream:
                return iter([fallback])
            return fallback
        finally:
            if project_token is not None:
                reset_runtime_project_context(project_token)
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)

    def _generate_with_json_tool_loop(
        self,
        user_input: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        original_request: Optional[str] = None,
        request_type: str = "tool",
    ) -> str:
        messages = self._build_json_tool_loop_messages(user_input)

        def _create(messages_payload: list[dict[str, Any]]) -> str:
            api_kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "messages": messages_payload,
                "temperature": temperature,
                "max_tokens": max_tokens or 1024,
                "response_format": {"type": "json_object"},
            }
            reasoning_effort = ollama_reasoning_effort_for_mode(
                self.model_name,
                self._current_llm_mode,
            )
            if reasoning_effort:
                api_kwargs["reasoning_effort"] = reasoning_effort
            response = self._create_completion_with_tool_fallback(
                api_kwargs,
                request_type=request_type,
            )
            return response.choices[0].message.content or ""

        result = run_json_tool_loop(
            create_completion=_create,
            initial_messages=messages,
            registry=filtered_registry_for_client(self, self._tool_registry),
            max_rounds=agentic_max_rounds(self, original_request or user_input),
            original_request=original_request or user_input,
            return_result=True,
            restore_tool_arguments=self._privacy_gateway.restore_tool_arguments,
        )
        return self._privacy_gateway.restore(
            guard_tool_execution_claims(result.final_output, result.tool_calls)
        )

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        original_request: str,
    ) -> str:
        if (
            get_client_generation_policy(self).discretionary_tool_loop_enabled
            and len(self._tool_registry) > 0
            and not self._native_tool_calling_enabled
        ):
            return self._generate_with_json_tool_loop(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                original_request=original_request,
                request_type="review",
            )

        messages = self._build_messages(prompt)
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            tools_enabled=self._native_tool_calling_enabled,
        )
        response = self._create_completion_with_tool_fallback(
            api_kwargs,
            request_type="review",
        )
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(
                messages=messages,
                assistant_message=choice.message,
                api_kwargs=api_kwargs,
                registry=filtered_registry_for_client(
                    self,
                    self._tool_registry,
                ),
                user_input=original_request,
            )
        return guard_tool_execution_claims(choice.message.content or "", [])

    def _create_completion_with_tool_fallback(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_type: str = "chat",
    ) -> Any:
        self._sync_privacy_gateway()
        api_kwargs = self._privacy_gateway.protect_sync(
            api_kwargs,
            provider="ollama",
            base_url=self.base_url,
            source_kind="model_request",
        ).payload
        started_at = time.monotonic()
        try:
            self._record_context_snapshot(api_kwargs)
            response = self.client.chat.completions.create(**api_kwargs)
            self._capture_and_persist_usage(
                response,
                request_type=request_type,
                latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            )
            return response
        except Exception as exc:
            optional_keys = {"tools", "tool_choice", "reasoning_effort", "reasoning"}
            if not optional_keys.intersection(api_kwargs):
                raise
            retry_kwargs = dict(api_kwargs)
            removed = []
            for key in optional_keys:
                if key in retry_kwargs:
                    retry_kwargs.pop(key, None)
                    removed.append(key)
            logger.warning(
                "[OllamaClient] Retrying without %s: %s",
                ", ".join(removed),
                exc,
            )
            self._record_context_snapshot(
                retry_kwargs,
                request_kind="chat.completions.retry",
            )
            retry_started_at = time.monotonic()
            response = self.client.chat.completions.create(**retry_kwargs)
            self._capture_and_persist_usage(
                response,
                request_type="retry",
                latency_ms=max(0, int((time.monotonic() - retry_started_at) * 1000)),
            )
            return response

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        registry: ToolRegistry,
        max_rounds: int = 5,
        user_input: Optional[str] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> str:
        effective_max_rounds = max(max_rounds, agentic_max_rounds(self, user_input))
        result = run_openai_tool_call_loop(
            initial_messages=messages,
            assistant_message=assistant_message,
            api_kwargs=api_kwargs,
            registry=registry,
            create_completion=lambda kwargs: self._create_completion_with_tool_fallback(
                kwargs,
                request_type="tool",
            ),
            log_prefix="OllamaClient",
            max_rounds=effective_max_rounds,
            return_result=True,
            config=self.config,
            user_input=user_input,
            event_callback=event_callback,
            restore_tool_arguments=self._privacy_gateway.restore_tool_arguments,
        )
        self._last_tool_loop_messages = [
            dict(message) for message in getattr(result, "messages", [])
        ]
        return guard_tool_execution_claims(result.final_output, result.tool_calls)

    def _stream_response(
        self,
        api_kwargs: Dict[str, Any],
        user_input: str,
    ) -> Generator[str, None, None]:
        self._sync_privacy_gateway()
        full_response = ""
        # Streaming usage belongs to this request only.  A stream can fail
        # before ``create`` returns (or produce no chunks at all), so do not
        # let the previous request's usage leak into the final persistence
        # hook below.
        self._last_usage = {}
        stream_usage: dict[str, Any] = {}
        stream_kwargs = dict(api_kwargs)
        try:
            stream_kwargs["stream"] = True
            stream_kwargs.pop("tools", None)
            stream_kwargs.pop("tool_choice", None)
            stream_kwargs = self._privacy_gateway.protect_sync(
                stream_kwargs,
                provider="ollama",
                base_url=self.base_url,
                source_kind="model_request",
            ).payload
            self._record_context_snapshot(
                stream_kwargs,
                request_kind="chat.completions.stream",
            )
            stream = self.client.chat.completions.create(**stream_kwargs)
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = self._privacy_gateway.restore_aliases(
                        chunk.choices[0].delta.content
                    )
                    full_response += content
                    yield content
                self._capture_usage(chunk)
                if self._last_usage:
                    stream_usage = dict(self._last_usage)
        except GenerationInterrupted:
            raise
        except Exception as exc:
            logger.error("[OllamaClient] streaming failed: %s", exc, exc_info=True)
            full_response = self._get_fallback_response()
            yield full_response
        finally:
            self._record_model_transcript(
                list(stream_kwargs.get("messages") or []), full_response
            )
            # ``_capture_usage`` may observe usage on a final metadata-only
            # chunk, so copy the latest confirmed value once more before
            # persisting.  Empty/failed streams keep this empty and therefore
            # do not create a duplicate row from a previous request.
            if self._last_usage:
                stream_usage = dict(self._last_usage)
            self._last_usage = dict(stream_usage)
            if stream_usage:
                persist_usage_sync(
                    self,
                    provider="ollama",
                    model=self.model_name,
                    usage=stream_usage,
                    request_type="chat",
                    is_streaming=True,
                )
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", full_response)

    def _get_fallback_response(self) -> str:
        if getattr(self, "config", None):
            try:
                character_config = self.config.get_character_config(self.character_name)
                personality = character_config.get("personality", {})
                fallback = personality.get("fallbackReply")
                if fallback:
                    return fallback
            except Exception:
                pass
        return "すみません。ローカルLLMの呼び出しでエラーが発生しました。"

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> str:
        from ..services.session_llm_generation import run_session_aware_generation

        return await run_session_aware_generation(
            self,
            self.config,
            user_input,
            temperature=temperature,
            max_tokens=max_tokens,
            image_data=image_data,
            stream_callback=stream_callback,
        )

    async def _generate_response_async_impl(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> str:
        # worker thread では実行中ループが見えないため、ここでループを束ねておく。
        return await asyncio.to_thread(
            self.generate_response,
            user_input,
            temperature,
            max_tokens,
            False,
            image_data,
            bind_stream_callback_loop(stream_callback),
        )

    def generate(self, prompt: str) -> str:
        return str(self.generate_response(prompt, stream=False))

    async def generate_async(self, prompt: str) -> str:
        return await self.generate_response_async(prompt)

    def clear_history(self) -> None:
        self.history_manager.clear()

    def get_history(self) -> List[Dict[str, str]]:
        return self.history_manager.get_all()

    async def cleanup(self) -> None:
        logger.info("[OllamaClient] cleanup complete")


def create_ollama_client(config: Config) -> OllamaClient:
    """Create an Ollama client for the configured local server."""
    ollama_config = _config_get(config, "ollama", {}) or {}
    response_model = (
        _config_get(config, "llm_model")
        if _config_get(config, "response_model_selection_active")
        else None
    )
    base_url = (
        _config_get(config, "runtime.target_base_url")
        or os.getenv("OLLAMA_BASE_URL")
        or _config_get(config, "ollama_base_url")
        or ollama_config.get("base_url")
        or DEFAULT_OLLAMA_BASE_URL
    )
    model = (
        _config_get(config, "runtime.target_model")
        or response_model
        or os.getenv("OLLAMA_MODEL")
        or _config_get(config, "ollama_model")
        or _config_get(config, "llm_model")
        or ollama_config.get("model")
        or DEFAULT_OLLAMA_MODEL
    )
    api_key = (
        _config_get(config, "runtime.target_api_key")
        or os.getenv("OLLAMA_API_KEY")
        or _config_get(config, "ollama_api_key")
        or ollama_config.get("api_key")
        or DEFAULT_OLLAMA_API_KEY
    )

    return OllamaClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config=config,
    )
