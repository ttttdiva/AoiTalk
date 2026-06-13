"""Generic local OpenAI-compatible LLM client.

This provider is for already-running local servers such as llama.cpp
llama-server. It automatically advertises registered tools and retries without
optional OpenAI-compatible parameters when a local server rejects them.
response_format and user-configured extra_body remain opt-in compatibility
parameters; mode-specific extra_body is applied automatically where needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import urllib.request
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
from .agent_runtime import (
    build_required_delegation_context_sync,
    compose_required_delegation_user_message,
    run_openai_tool_call_loop,
)
from .prompts import build_unified_instructions
from .provider_capabilities import ProviderCapabilities
from .runtime_tool_registry import build_runtime_tool_registry
from .tool_policy import reset_current_user_input, set_current_user_input

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_API_KEY = "dummy"
LOCAL_MODEL_LOADING_RESPONSE = (
    "ローカルLLMはモデルを読み込み中です。少し待ってからもう一度送信してください。"
)


def _is_qwopus_model(model: str) -> bool:
    return "qwopus" in str(model or "").strip().lower()


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _strip_leading_think_markup(text: str) -> str:
    value = text.strip()
    while value.startswith("</think>"):
        value = value[len("</think>") :].lstrip()
    if value.startswith("<think>") and "</think>" in value:
        value = value.split("</think>", 1)[1].lstrip()
    return value


def _config_get(config: Optional[Config], key: str, default: Any = None) -> Any:
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


def _normalize_base_url(base_url: str) -> str:
    clean = (base_url or DEFAULT_LOCAL_BASE_URL).rstrip("/")
    if clean.endswith("/v1"):
        return clean
    return f"{clean}/v1"


def _is_local_model_loading_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)

    if status_code != 503:
        return False

    body = getattr(exc, "body", None)
    text = f"{exc} {body}".lower()
    return "loading model" in text or "unavailable_error" in text


class OpenAICompatibleLocalClient:
    """Client for generic local OpenAI-compatible chat completion servers."""

    def __init__(
        self,
        base_url: str = DEFAULT_LOCAL_BASE_URL,
        model: str = DEFAULT_LOCAL_MODEL,
        api_key: str = DEFAULT_LOCAL_API_KEY,
        config: Optional[Config] = None,
        *,
        enable_tools: bool = False,
        enable_response_format: bool = False,
        enable_extra_body: bool = False,
        extra_body: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.base_url = _normalize_base_url(base_url)
        self.model_name = model or DEFAULT_LOCAL_MODEL
        self.api_key = api_key or DEFAULT_LOCAL_API_KEY
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self.enable_tools = bool(enable_tools)
        self.enable_response_format = bool(enable_response_format)
        self.enable_extra_body = bool(enable_extra_body)
        self.extra_body = extra_body if isinstance(extra_body, dict) else {}
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_response_format=True,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=True,
        )

        if hasattr(config, "default_character"):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get("default_character", "Assistant")
        else:
            self.character_name = "Assistant"

        self.history_manager = HistoryManager()
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

        logger.info("[OpenAICompatibleLocalClient] initialized")
        logger.info("[OpenAICompatibleLocalClient] Base URL: %s", self.base_url)
        logger.info("[OpenAICompatibleLocalClient] Model: %s", self.model_name)
        logger.info(
            "[OpenAICompatibleLocalClient] tools=auto response_format=%s extra_body=%s",
            self.enable_response_format,
            self.enable_extra_body,
        )

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
            logger.debug(
                "[OpenAICompatibleLocalClient] Failed to load custom instructions: %s",
                exc,
            )
            custom_instructions = ""
        try:
            return build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                custom_instructions=custom_instructions,
            )
        except Exception as exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] Falling back to basic prompt: %s", exc
            )
            return f"あなたは{self.character_name}です。"

    def set_character(self, character_name: str) -> None:
        self.character_name = character_name
        self.system_prompt = self._build_system_prompt()

    def update_character(self, yaml_filename: str) -> None:
        if not self.config:
            return
        character_config = self.config.get_character_config(yaml_filename)
        self.character_name = character_config.get("name", yaml_filename)
        self.clear_history()
        self.system_prompt = self._build_system_prompt()

    def set_system_prompt(self, prompt: str) -> None:
        if self.config:
            self.system_prompt = self._build_system_prompt()
            return
        self.system_prompt = prompt

    def set_llm_mode(self, mode: str) -> None:
        if mode not in {"fast", "thinking"}:
            mode = "fast"
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
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_input})
        return messages

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
            logger.warning(
                "[OpenAICompatibleLocalClient] Failed to resolve project context: %s",
                exc,
            )
            return None

    def _build_required_delegation_context(self, user_input: str) -> str:
        return build_required_delegation_context_sync(
            user_input=user_input,
            registry=self._tool_registry,
            policy=get_client_generation_policy(self),
            log_prefix="OpenAICompatibleLocalClient",
        )

    def _mode_extra_body(self) -> Dict[str, Any]:
        if not _is_qwopus_model(self.model_name):
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": self._current_llm_mode == "thinking",
            }
        }

    def _set_qwopus_thinking(
        self, api_kwargs: Dict[str, Any], enabled: bool
    ) -> Dict[str, Any]:
        retry_kwargs = dict(api_kwargs)
        extra_body = retry_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        retry_kwargs["extra_body"] = _deep_merge_dict(
            extra_body,
            {"chat_template_kwargs": {"enable_thinking": enabled}},
        )
        return retry_kwargs

    def _with_stream_safe_extra_body(self, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if not _is_qwopus_model(self.model_name):
            return api_kwargs
        return self._set_qwopus_thinking(api_kwargs, False)

    def _build_api_kwargs(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        api_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 1024,
        }
        tools = self._chat_completion_tools()
        if tools:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(tools)
            api_kwargs["tool_choice"] = "auto"
        if self.enable_response_format:
            api_kwargs["response_format"] = {"type": "json_object"}
        extra_body: Dict[str, Any] = {}
        if self.enable_extra_body and self.extra_body:
            extra_body = _deep_merge_dict(extra_body, self.extra_body)
        mode_extra_body = self._mode_extra_body()
        if mode_extra_body:
            extra_body = _deep_merge_dict(extra_body, mode_extra_body)
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        return api_kwargs

    def _chat_completion_tools(self) -> List[Any]:
        if len(self._tool_registry) <= 0:
            return []
        return self._tool_registry.get_all()

    def _create_completion_with_fallback(self, api_kwargs: Dict[str, Any]) -> Any:
        try:
            return self.client.chat.completions.create(**api_kwargs)
        except Exception as exc:
            if _is_local_model_loading_error(exc):
                raise
            retry_kwargs = dict(api_kwargs)
            removed = []
            for key in ("tools", "tool_choice", "response_format", "extra_body"):
                if key in retry_kwargs:
                    retry_kwargs.pop(key, None)
                    removed.append(key)
            if not removed:
                raise
            logger.warning(
                "[OpenAICompatibleLocalClient] Retrying without %s: %s",
                ", ".join(removed),
                exc,
            )
            return self.client.chat.completions.create(**retry_kwargs)

    def _message_content(self, message: Any) -> str:
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            return ""
        value = content.strip()
        if _is_qwopus_model(self.model_name):
            value = _strip_leading_think_markup(value)
        return value

    def _should_retry_without_thinking(
        self, message: Any, api_kwargs: Dict[str, Any]
    ) -> bool:
        if not _is_qwopus_model(self.model_name):
            return False
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return False
        template_kwargs = extra_body.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            return False
        if template_kwargs.get("enable_thinking") is not True:
            return False
        reasoning_content = getattr(message, "reasoning_content", None)
        return isinstance(reasoning_content, str) and bool(reasoning_content.strip())

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        api_kwargs = self._build_api_kwargs(messages, temperature, max_tokens)
        response = self._create_completion_with_fallback(api_kwargs)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(messages, choice.message, api_kwargs)
        content = self._message_content(choice.message)
        if content:
            return content
        if self._should_retry_without_thinking(choice.message, api_kwargs):
            logger.warning(
                "[OpenAICompatibleLocalClient] Empty Qwopus content with reasoning output; retrying with thinking disabled"
            )
            retry_kwargs = self._set_qwopus_thinking(api_kwargs, False)
            response = self._create_completion_with_fallback(retry_kwargs)
            choice = response.choices[0]
            if getattr(choice.message, "tool_calls", None):
                return self._handle_tool_calls(messages, choice.message, retry_kwargs)
            return self._message_content(choice.message)
        return ""

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        stream_kwargs = self._build_api_kwargs(messages, temperature, max_tokens)
        stream_kwargs["stream"] = True
        stream_kwargs.pop("tools", None)
        stream_kwargs.pop("tool_choice", None)
        stream_kwargs.pop("response_format", None)
        stream_kwargs = self._with_stream_safe_extra_body(stream_kwargs)
        stream = self.client.chat.completions.create(**stream_kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        max_rounds: int = 5,
    ) -> str:
        return run_openai_tool_call_loop(
            initial_messages=messages,
            assistant_message=assistant_message,
            api_kwargs=api_kwargs,
            registry=self._tool_registry,
            create_completion=self._create_completion_with_fallback,
            log_prefix="OpenAICompatibleLocalClient",
            max_rounds=max_rounds,
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
            logger.warning(
                "[OpenAICompatibleLocalClient] image_data is ignored by this client"
            )
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        policy = get_client_generation_policy(self)
        generation_policy_token = set_current_generation_policy(policy)
        try:
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            required_delegation_context = self._build_required_delegation_context(
                user_input
            )
            model_user_input = compose_required_delegation_user_message(
                user_input,
                required_delegation_context,
            )

            messages = self._build_messages(model_user_input)
            if stream:
                return self._stream_response(messages, temperature, max_tokens, user_input)
            response_text = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response_text)
            return response_text
        except Exception as exc:
            fallback = self._get_error_response(exc)
            if _is_local_model_loading_error(exc):
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model is still loading: %s",
                    exc,
                )
            else:
                logger.error(
                    "[OpenAICompatibleLocalClient] generation failed: %s",
                    exc,
                    exc_info=True,
                )
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

    def _stream_response(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        user_input: str,
    ) -> Generator[str, None, None]:
        full_response = ""
        try:
            for content in self.stream_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                full_response += content
                yield content
        except Exception as exc:
            full_response = self._get_error_response(exc)
            if _is_local_model_loading_error(exc):
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model is still loading during streaming: %s",
                    exc,
                )
            else:
                logger.error(
                    "[OpenAICompatibleLocalClient] streaming failed: %s",
                    exc,
                    exc_info=True,
                )
            yield full_response
        finally:
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", full_response)

    def _get_error_response(self, exc: Exception) -> str:
        if _is_local_model_loading_error(exc):
            return LOCAL_MODEL_LOADING_RESPONSE
        return self._get_fallback_response()

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

    def list_models(self) -> List[Dict[str, Any]]:
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [
            item
            for item in data.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]

    def health_check(self) -> Dict[str, Any]:
        try:
            models = self.list_models()
            return {
                "ok": True,
                "base_url": self.base_url,
                "model_count": len(models),
            }
        except Exception as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc)}

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
        logger.info("[OpenAICompatibleLocalClient] cleanup complete")


def create_openai_compatible_local_client(config: Config) -> OpenAICompatibleLocalClient:
    local_config = _config_get(config, "openai_compatible_local", {}) or {}
    response_model = (
        _config_get(config, "llm_model")
        if _config_get(config, "response_model_selection_active")
        else None
    )
    base_url = (
        os.getenv("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
        or _config_get(config, "openai_compatible_local.base_url")
        or DEFAULT_LOCAL_BASE_URL
    )
    model = (
        response_model
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_MODEL")
        or _config_get(config, "llm_model")
        or _config_get(config, "openai_compatible_local.model")
        or DEFAULT_LOCAL_MODEL
    )
    api_key = (
        os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
        or _config_get(config, "openai_compatible_local.api_key")
        or DEFAULT_LOCAL_API_KEY
    )

    return OpenAICompatibleLocalClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        config=config,
        enable_tools=bool(local_config.get("enable_tools", False)),
        enable_response_format=bool(
            local_config.get("enable_response_format", False)
        ),
        enable_extra_body=bool(local_config.get("enable_extra_body", False)),
        extra_body=local_config.get("extra_body") or {},
    )

