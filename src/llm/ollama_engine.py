"""Ollama local LLM client using Ollama's OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from typing import Any, Dict, Generator, List, Optional, Union

from openai import OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..services.project_context import (
    ProjectContextResolver,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..tools.adapters import OpenAIAPIAdapter
from ..tools.registry import ToolRegistry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
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
from .provider_mode_adapters import (
    ollama_mode_options_for_model,
    ollama_reasoning_effort_for_mode,
)
from .runtime_tool_registry import build_runtime_tool_registry
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
        self.base_url = _normalize_base_url(base_url)
        self.model_name = model or DEFAULT_OLLAMA_MODEL
        self.api_key = api_key or DEFAULT_OLLAMA_API_KEY
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
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
            self._tool_registry = build_runtime_tool_registry(config)
        else:
            self._tool_registry = ToolRegistry()

        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_llm_mode = "fast"

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
                custom_instructions=custom_instructions,
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
        self.system_prompt = self._build_system_prompt()

    def _build_messages(self, user_input: str) -> List[Dict[str, str]]:
        messages = [{"role": "system", "content": self.system_prompt}]
        context_window = self.history_manager.context_window_size
        for msg in self.history_manager.get_all()[-(context_window * 2) :]:
            messages.append(
                {
                    "role": msg["role"],
                    "content": msg["content"],
                }
            )
        messages.append({"role": "user", "content": user_input})
        return messages

    def _build_json_tool_user_message(self, user_input: str) -> str:
        return "\n".join(
            [
                "User request:",
                user_input,
                "",
                "Decide whether a tool is needed.",
                (
                    "If the request explicitly asks for web search or uses Japanese terms such "
                    "as 調べて or 調査して, use a relevant search tool first."
                ),
                (
                    "When calling a tool with a `request` parameter, copy the user's request exactly "
                    "unless a shorter accurate query is obvious."
                ),
            ]
        )

    def _build_json_tool_loop_messages(self, user_input: str) -> List[Dict[str, str]]:
        system_prompt = build_json_tool_loop_system_prompt(
            self.system_prompt,
            self._tool_registry,
        )
        messages = [{"role": "system", "content": system_prompt}]
        context_window = self.history_manager.context_window_size
        for msg in self.history_manager.get_all()[-(context_window * 2) :]:
            messages.append(
                {
                    "role": msg["role"],
                    "content": msg["content"],
                }
            )
        messages.append(
            {"role": "user", "content": self._build_json_tool_user_message(user_input)}
        )
        return messages

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
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(
                self._tool_registry.get_all()
            )
            api_kwargs["tool_choice"] = "auto"

        return api_kwargs

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        api_kwargs = self._build_api_kwargs(messages, temperature, max_tokens)
        response = self._create_completion_with_tool_fallback(api_kwargs)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(
                messages=messages,
                assistant_message=choice.message,
                api_kwargs=api_kwargs,
                registry=self._tool_registry,
            )
        return choice.message.content or ""

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            tools_enabled=False,
        )
        api_kwargs["stream"] = True
        stream = self.client.chat.completions.create(**api_kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

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
                )
            )
        except Exception as exc:
            logger.warning("[OllamaClient] Failed to resolve project context: %s", exc)
            return None

    def _build_tool_hint_context(self, user_input: str) -> str:
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=self._tool_registry,
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
        if image_data:
            logger.warning("[OllamaClient] image_data is ignored by this client")

        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        policy = get_client_generation_policy(self)
        generation_policy_token = set_current_generation_policy(policy)
        try:
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            model_user_input = user_input
            tool_hint_context = self._build_tool_hint_context(
                user_input
            )
            model_user_input = compose_tool_hint_user_message(
                user_input,
                tool_hint_context,
            )

            if (
                policy.discretionary_tool_loop_enabled
                and not stream
                and len(self._tool_registry) > 0
                and not self._native_tool_calling_enabled
            ):
                response_text = self._generate_with_json_tool_loop(
                    model_user_input,
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
                        [{"role": "user", "content": model_user_input}]
                    ),
                    user_input=user_input,
                    initial_response=response_text,
                )
                self.history_manager.add_message("user", user_input)
                self.history_manager.add_message("assistant", response_text)
                return response_text

            messages = self._build_messages(model_user_input)
            api_kwargs = self._build_api_kwargs(
                messages,
                temperature,
                max_tokens,
                tools_enabled=self._native_tool_calling_enabled,
            )

            if stream:
                return self._stream_response(api_kwargs, user_input)

            response = self._create_completion_with_tool_fallback(api_kwargs)
            choice = response.choices[0]
            if getattr(choice.message, "tool_calls", None):
                response_text = self._handle_tool_calls(
                    messages=messages,
                    assistant_message=choice.message,
                    api_kwargs=api_kwargs,
                    registry=self._tool_registry,
                    user_input=user_input,
                )
            elif project_progress_review_active(user_input):
                response_text = self._handle_tool_calls(
                    messages=messages,
                    assistant_message=choice.message,
                    api_kwargs=api_kwargs,
                    registry=self._tool_registry,
                    user_input=user_input,
                )
            else:
                response_text = guard_tool_execution_claims(
                    choice.message.content or "",
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
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response_text)
            return response_text
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
            response = self.client.chat.completions.create(**api_kwargs)
            return response.choices[0].message.content or ""

        result = run_json_tool_loop(
            create_completion=_create,
            initial_messages=messages,
            registry=self._tool_registry,
            max_rounds=agentic_max_rounds(self, original_request or user_input),
            original_request=original_request or user_input,
            return_result=True,
        )
        return guard_tool_execution_claims(result.final_output, result.tool_calls)

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
            )

        messages = self._build_messages(prompt)
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            tools_enabled=self._native_tool_calling_enabled,
        )
        response = self._create_completion_with_tool_fallback(api_kwargs)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(
                messages=messages,
                assistant_message=choice.message,
                api_kwargs=api_kwargs,
                registry=self._tool_registry,
                user_input=original_request,
            )
        return guard_tool_execution_claims(choice.message.content or "", [])

    def _create_completion_with_tool_fallback(self, api_kwargs: Dict[str, Any]) -> Any:
        try:
            return self.client.chat.completions.create(**api_kwargs)
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
            return self.client.chat.completions.create(**retry_kwargs)

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        registry: ToolRegistry,
        max_rounds: int = 5,
        user_input: Optional[str] = None,
    ) -> str:
        effective_max_rounds = max(max_rounds, agentic_max_rounds(self, user_input))
        result = run_openai_tool_call_loop(
            initial_messages=messages,
            assistant_message=assistant_message,
            api_kwargs=api_kwargs,
            registry=registry,
            create_completion=self._create_completion_with_tool_fallback,
            log_prefix="OllamaClient",
            max_rounds=effective_max_rounds,
            return_result=True,
            config=self.config,
            user_input=user_input,
        )
        return guard_tool_execution_claims(result.final_output, result.tool_calls)

    def _stream_response(
        self,
        api_kwargs: Dict[str, Any],
        user_input: str,
    ) -> Generator[str, None, None]:
        full_response = ""
        try:
            stream_kwargs = dict(api_kwargs)
            stream_kwargs["stream"] = True
            stream_kwargs.pop("tools", None)
            stream_kwargs.pop("tool_choice", None)
            stream = self.client.chat.completions.create(**stream_kwargs)
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield content
        except Exception as exc:
            logger.error("[OllamaClient] streaming failed: %s", exc, exc_info=True)
            full_response = self._get_fallback_response()
            yield full_response
        finally:
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", full_response)

    def _get_fallback_response(self) -> str:
        if self.config:
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
        return await asyncio.to_thread(
            self.generate_response,
            user_input,
            temperature,
            max_tokens,
            False,
            image_data,
            stream_callback,
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
        os.getenv("OLLAMA_BASE_URL")
        or _config_get(config, "ollama_base_url")
        or ollama_config.get("base_url")
        or DEFAULT_OLLAMA_BASE_URL
    )
    model = (
        response_model
        or os.getenv("OLLAMA_MODEL")
        or _config_get(config, "ollama_model")
        or _config_get(config, "llm_model")
        or ollama_config.get("model")
        or DEFAULT_OLLAMA_MODEL
    )
    api_key = (
        os.getenv("OLLAMA_API_KEY")
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
