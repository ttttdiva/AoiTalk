"""Generic local OpenAI-compatible LLM client.

This provider is for already-running local servers such as llama.cpp
llama-server. Optional OpenAI-compatible features such as native tool calling,
response_format, and user-configured extra_body are opt-in compatibility
parameters; mode-specific extra_body is applied automatically where needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import urllib.request
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Union

from openai import OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..services.project_context import (
    ProjectContextResolver,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..tools.adapters import OpenAIAPIAdapter
from ..tools.registry import ToolRegistry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    GenerationProfile,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .agentic_completion import (
    agentic_max_rounds,
    render_messages_for_review,
    run_agentic_completion_loop_sync,
)
from .agent_runtime import (
    DIRECT_FILESYSTEM_TOOL_HINT_NAMES,
    DIRECT_PROJECT_TOOL_HINT_NAMES,
    DIRECT_SEARCH_TOOL_HINT_NAMES,
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
    project_context_required_read_tool_names,
    run_openai_tool_call_loop,
)
from .specialist_delegate import (
    reset_runtime_specialist_provider,
    set_runtime_specialist_provider,
)
from .context_budget import (
    ContextBudget,
    clip_text,
    clip_text_preserve_tail,
    reduced_context_window_after_overflow,
    resolve_context_budget,
)
from .openai_compatible_local_profiles import (
    DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL,
    openai_compatible_local_base_url,
)
from .prompts import build_unified_instructions
from .provider_capabilities import ProviderCapabilities
from .runtime_tool_registry import build_runtime_tool_registry
from .tool_policy import (
    command_capability_active,
    looks_like_bare_search_followup_request,
    looks_like_filesystem_request,
    looks_like_media_request,
    looks_like_project_management_request,
    looks_like_search_request,
    looks_like_utility_request,
    project_management_required_mutation_tools,
    project_progress_review_active,
    reset_current_user_input,
    set_current_user_input,
)

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_API_KEY = "dummy"
CONSTRAINED_NATIVE_TOOL_SCHEMA_CHAR_BUDGET = 9000
LOCAL_MODEL_LOADING_RESPONSE = (
    "ローカルLLMはモデルを読み込み中です。少し待ってからもう一度送信してください。"
)
LOCAL_MODEL_CONTEXT_OVERFLOW_RESPONSE = (
    "ローカルLLMのコンテキスト上限を超えたため、通常の応答生成に失敗しました。"
    "プロンプトを圧縮して再試行しましたが成功しなかったため、"
    "AoiTalk側で取得済みの短い根拠だけを表示します。"
)
LOCAL_MODEL_EMPTY_RESPONSE = (
    "ローカルLLMから本文のない応答が返りました。"
    "モデルのthinking設定、コンテキスト量、またはOpenAI互換サーバーの応答形式を確認してください。"
)
TITLE_GENERATION_SYSTEM_PROMPT = (
    "You generate concise Japanese chat history titles. "
    "Return only the title, without explanations, quotes, markdown, or labels."
)
REASONING_OUTPUT_FIELDS = (
    "reasoning_content",
    "reasoning",
    "thinking_content",
)


def _current_date_context() -> str:
    now = datetime.now().astimezone()
    timezone_name = now.tzname() or "local time"
    return (
        "Current date context:\n"
        f"- Today is {now.date().isoformat()} ({now.strftime('%A')}, {timezone_name}).\n"
        "- Resolve relative dates such as today, tomorrow, this week, and 今週 against this date.\n"
        "- Treat this week as Monday through Sunday unless the user specifies another range."
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


def _message_field(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message.get(field)
    value = getattr(message, field, None)
    if value is not None:
        return value
    if hasattr(message, "model_dump"):
        try:
            dumped = message.model_dump()
            if isinstance(dumped, dict):
                return dumped.get(field)
        except Exception:
            return None
    return None


def _message_field_text(message: Any, field: str) -> str:
    value = _message_field(message, field)
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    text = str(value).strip()
    return text if text and text != "None" else ""


def _message_has_reasoning_output(message: Any) -> bool:
    return any(_message_field_text(message, field) for field in REASONING_OUTPUT_FIELDS)


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


def _is_context_overflow_error(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    text = f"{exc} {body}".casefold()
    overflow_terms = (
        "context size",
        "context length",
        "context window",
        "maximum context",
        "max context",
        "n_ctx",
        "too many tokens",
        "prompt is too long",
        "requested tokens",
        "exceeds context",
        "exceeded context",
        "exceeds the context",
        "exceeded the context",
        "exceeds maximum context",
        "コンテキスト",
    )
    return any(term in text for term in overflow_terms)


def _as_plain_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    result: Dict[str, Any] = {}
    for attr in ("timings", "usage", "model", "id"):
        if hasattr(value, attr):
            result[attr] = getattr(value, attr)
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, dict):
        result.update(extra)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        result.update(
            {key: val for key, val in attrs.items() if not key.startswith("_")}
        )
    return result


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _as_finite_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _as_non_negative_int(value: Any) -> Optional[int]:
    number = _as_finite_float(value)
    if number is None or number < 0:
        return None
    return int(number)


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
        self._current_session_id: Optional[str] = None
        self._history_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.current_include_project_context: Optional[bool] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._current_context_budget: Optional[ContextBudget] = None
        self._context_window_override_tokens: Optional[int] = None
        self._current_tool_hint_context = ""
        self._last_generation_metrics: Optional[Dict[str, Any]] = None
        self._last_tool_calls: List[Any] = []
        self._last_agentic_events: List[Dict[str, Any]] = []
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

    def _get_memory_metadata(self) -> Dict[str, Any]:
        return self.session_metadata.copy() if self.session_metadata else {}

    def get_generation_metadata(self) -> Dict[str, Any]:
        if not self._last_generation_metrics:
            return {}
        return {"generation_metrics": dict(self._last_generation_metrics)}

    def _capture_generation_metrics(self, response: Any) -> None:
        payload = _as_plain_dict(response)
        timings = _as_plain_dict(payload.get("timings"))
        usage = _as_plain_dict(payload.get("usage"))

        tokens_per_second = _as_finite_float(
            _first_present(
                timings.get("predicted_per_second"),
                timings.get("completion_per_second"),
                timings.get("eval_per_second"),
            )
        )
        output_tokens = _as_non_negative_int(
            _first_present(
                timings.get("predicted_n"),
                usage.get("completion_tokens"),
                usage.get("output_tokens"),
            )
        )
        prompt_tokens = _as_non_negative_int(
            _first_present(usage.get("prompt_tokens"), usage.get("input_tokens"))
        )
        total_tokens = _as_non_negative_int(usage.get("total_tokens"))
        predicted_ms = _as_finite_float(timings.get("predicted_ms"))
        prompt_ms = _as_finite_float(timings.get("prompt_ms"))

        if tokens_per_second is None and output_tokens and predicted_ms and predicted_ms > 0:
            tokens_per_second = output_tokens / (predicted_ms / 1000)

        if tokens_per_second is None and output_tokens is None:
            self._last_generation_metrics = None
            return

        metrics: Dict[str, Any] = {
            "provider": "openai_compatible_local",
            "model": self.model_name,
        }
        if tokens_per_second is not None and tokens_per_second >= 0:
            metrics["tokens_per_second"] = round(tokens_per_second, 2)
        if output_tokens is not None:
            metrics["output_tokens"] = output_tokens
        if prompt_tokens is not None:
            metrics["prompt_tokens"] = prompt_tokens
        if total_tokens is not None:
            metrics["total_tokens"] = total_tokens
        if predicted_ms is not None:
            metrics["generation_ms"] = round(predicted_ms, 3)
        if prompt_ms is not None:
            metrics["prompt_ms"] = round(prompt_ms, 3)
        self._last_generation_metrics = metrics

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

    @property
    def current_session_id(self) -> Optional[str]:
        return self._current_session_id

    @current_session_id.setter
    def current_session_id(self, value: Optional[str]) -> None:
        normalized = str(value).strip() if value else None
        if normalized and normalized != self._history_session_id:
            if hasattr(self, "history_manager"):
                self.history_manager.clear()
            self._history_session_id = normalized
            self._context_window_override_tokens = None
            logger.debug(
                "[OpenAICompatibleLocalClient] Cleared local history for session switch: %s",
                normalized,
            )
        self._current_session_id = normalized

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

    def _include_project_context_enabled(self) -> bool:
        return getattr(self, "current_include_project_context", True) is not False

    def _context_budget(self, max_tokens: Optional[int] = None) -> ContextBudget:
        return resolve_context_budget(
            config=self.config,
            provider_key="openai_compatible_local",
            base_url=self.base_url,
            model_name=self.model_name,
            api_key=self.api_key,
            requested_max_tokens=max_tokens,
            override_context_window_tokens=self._context_window_override_tokens,
        )

    def _build_messages(
        self,
        user_input: str,
        context_budget: ContextBudget,
    ) -> List[Dict[str, str]]:
        system_prompt = "\n\n".join(
            [
                self._system_prompt_for_budget(context_budget),
                _current_date_context(),
            ]
        )
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        context_window = self.history_manager.context_window_size
        history_limit = min(context_window * 2, context_budget.history_messages)
        for msg in self.history_manager.get_all()[-history_limit:]:
            messages.append(
                {
                    "role": msg["role"],
                    "content": clip_text(
                        str(msg["content"]),
                        context_budget.history_message_chars,
                    ),
                }
            )
        messages.append(
            {
                "role": "user",
                "content": clip_text_preserve_tail(
                    user_input,
                    context_budget.message_budget_chars,
                ),
            }
        )
        return self._fit_messages_to_context_budget(messages, context_budget)

    def _fit_messages_to_context_budget(
        self,
        messages: List[Dict[str, str]],
        context_budget: ContextBudget,
    ) -> List[Dict[str, str]]:
        def payload_chars(items: List[Dict[str, str]]) -> int:
            return sum(len(str(item.get("content") or "")) + 24 for item in items)

        compacted = list(messages)
        while (
            len(compacted) > 2
            and payload_chars(compacted) > context_budget.message_budget_chars
        ):
            compacted.pop(1)
        if payload_chars(compacted) > context_budget.message_budget_chars and compacted:
            compacted[-1] = {
                **compacted[-1],
                "content": clip_text_preserve_tail(
                    compacted[-1].get("content", ""),
                    max(
                        1000,
                        context_budget.message_budget_chars
                        - len(compacted[0].get("content", ""))
                        - 48,
                    ),
                ),
            }
        return compacted

    def _is_constrained_context_budget(self, context_budget: ContextBudget) -> bool:
        return context_budget.context_window_tokens <= 16384

    def _system_prompt_for_budget(self, context_budget: ContextBudget) -> str:
        if not self._is_constrained_context_budget(context_budget):
            return self.system_prompt
        return "\n".join(
            [
                "You are AoiTalk's assistant. Answer in the user's language.",
                "Use selected project context, project information DB, and confirmed tool results as ground truth.",
                "Use tool hints as short candidate reminders and decide tool use from the full request.",
                "Do not claim search, file reading, project updates, time/weather lookup, or calculation unless a successful tool result exists.",
                "Do not invent case details. If evidence is insufficient, say what is missing and answer only from available facts.",
                "For file or workspace questions, trust successful filesystem inspection results included in the user message.",
                "Do not claim a file could not be read when the provided evidence says it was read.",
            ]
        )

    def _context_block_for_budget(
        self,
        context_bundle: Optional[ContextBundle],
        context_budget: ContextBudget,
        *,
        has_tool_hints: bool,
    ) -> str:
        if not context_bundle:
            return ""
        if not self._is_constrained_context_budget(context_budget):
            return context_bundle.render_for_prompt(context_budget.context_bundle_chars)
        if has_tool_hints:
            blocks = [
                context_bundle.project_context_block,
                context_bundle.project_information_block,
            ]
            compact = "\n\n".join(
                block.strip() for block in blocks if block and block.strip()
            )
            return clip_text(compact, min(1400, context_budget.context_bundle_chars))
        return context_bundle.render_for_prompt(context_budget.context_bundle_chars)

    def _build_model_messages_for_budget(
        self,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        context_budget: ContextBudget,
    ) -> tuple[List[Dict[str, str]], str]:
        self._current_context_bundle = self._build_context_bundle_sync(
            user_input,
            project_context,
            context_budget,
        )
        tool_hint_context = self._build_tool_hint_context(
            user_input,
            context_budget,
        )
        self._current_tool_hint_context = tool_hint_context
        model_user_input = compose_tool_hint_user_message(
            user_input,
            tool_hint_context,
        )
        context_block = self._context_block_for_budget(
            self._current_context_bundle,
            context_budget,
            has_tool_hints=bool(tool_hint_context),
        )
        if context_block:
            model_user_input = f"{context_block}\n\n{model_user_input}"
        return (
            self._build_messages(model_user_input, context_budget),
            tool_hint_context,
        )

    def _retry_after_context_overflow(
        self,
        *,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        required_tool_name: Optional[str] = None,
    ) -> str:
        for _ in range(2):
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            try:
                messages, tool_hint_context = self._build_model_messages_for_budget(
                    user_input,
                    project_context,
                    context_budget,
                )
                return self.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools_enabled=bool(required_tool_name),
                    context_budget=context_budget,
                    fallback_user_input=user_input,
                    required_tool_name=required_tool_name,
                    native_tool_user_input=user_input,
                )
            except Exception as retry_exc:
                if not _is_context_overflow_error(retry_exc):
                    raise
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] compact retry still exceeded context: %s",
                    retry_exc,
                )
        try:
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            return self._plain_empty_response_retry(
                user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="context overflow",
            )
        except Exception as final_retry_exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] minimal overflow retry failed: %s",
                final_retry_exc,
            )
        return ""

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

    def _build_tool_hint_context(
        self,
        user_input: str,
        context_budget: ContextBudget,
    ) -> str:
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=self._tool_registry,
            policy=get_client_generation_policy(self),
            log_prefix="OpenAICompatibleLocalClient",
            max_result_chars=context_budget.tool_hint_context_chars,
        )

    def _required_command_tool_name(self, user_input: str) -> Optional[str]:
        if "web_search" in set(
            getattr(self, "current_command_capabilities", ()) or ()
        ):
            return "web_search"
        if command_capability_active(user_input, "web_search"):
            return "web_search"
        return None

    def _required_project_management_tool_name(
        self,
        user_input: str,
    ) -> Optional[str]:
        policy = get_client_generation_policy(self)
        if policy.profile != GenerationProfile.AUTONOMOUS_WORK:
            return None
        if not (self.current_project_id or self.current_session_id):
            return None

        required_tools = project_management_required_mutation_tools(user_input)
        for tool_name in (
            "organize_project_information_from_folder",
            "sync_wbs_tasks",
            "sync_issue_table",
            "upsert_project_fact",
            "create_record_table",
            "create_task",
            "update_task",
        ):
            if tool_name in required_tools and tool_name in self._tool_registry:
                return tool_name
        return None

    def _requires_project_context_read(self, user_input: str | None) -> bool:
        return (
            getattr(self, "current_include_project_context", None) is True
            and not looks_like_bare_search_followup_request(user_input or "")
            and bool(self.current_project_id or self.current_session_id)
        )

    def _required_project_context_read_tool_name(
        self,
        user_input: str,
    ) -> Optional[str]:
        if not self._requires_project_context_read(user_input):
            return None
        for tool_name in project_context_required_read_tool_names(self._tool_registry):
            return tool_name
        return None

    def _required_tool_name(self, user_input: str) -> Optional[str]:
        return self._required_command_tool_name(
            user_input
        ) or self._required_project_context_read_tool_name(
            user_input
        ) or self._required_project_management_tool_name(user_input)

    def _build_context_bundle_sync(
        self,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        context_budget: ContextBudget,
    ) -> Optional[ContextBundle]:
        include_project_context = (
            self._include_project_context_enabled()
            and not looks_like_bare_search_followup_request(user_input)
        )
        if (
            include_project_context
            and not project_context
            and not self.current_project_id
            and not self.current_session_id
        ):
            return None
        if not include_project_context and not self.current_session_id:
            return None
        try:
            return self._run_async_sync(
                ContextBuilder().build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id if include_project_context else None,
                    session_id=self.current_session_id,
                    project_context=project_context if include_project_context else None,
                    include_project_context=include_project_context,
                    max_chars=context_budget.context_bundle_chars,
                )
            )
        except Exception as exc:
            logger.warning(
                "[OpenAICompatibleLocalClient] ContextBuilder failed: %s",
                exc,
            )
            return None

    def _mode_extra_body(self) -> Dict[str, Any]:
        if not _is_qwopus_model(self.model_name):
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": self._current_llm_mode == "thinking",
            }
        }

    def _set_chat_template_thinking(
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

    def _set_qwopus_thinking(
        self, api_kwargs: Dict[str, Any], enabled: bool
    ) -> Dict[str, Any]:
        return self._set_chat_template_thinking(api_kwargs, enabled)

    def _with_stream_safe_extra_body(self, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if not _is_qwopus_model(self.model_name):
            return api_kwargs
        return self._set_chat_template_thinking(api_kwargs, False)

    def _build_api_kwargs(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        context_budget: Optional[ContextBudget] = None,
        tools_enabled: bool = True,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
    ) -> Dict[str, Any]:
        budget = context_budget or self._context_budget(max_tokens)
        response_tokens = (
            min(max_tokens, budget.response_tokens)
            if max_tokens
            else budget.response_tokens
        )
        api_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": response_tokens,
        }
        tools = (
            self._chat_completion_tools(
                context_budget=budget,
                required_tool_name=required_tool_name,
                user_input=native_tool_user_input,
            )
            if tools_enabled
            else []
        )
        if tools:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(tools)
            available_names = {str(getattr(tool_def, "name", "")) for tool_def in tools}
            if required_tool_name and required_tool_name in available_names:
                api_kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": required_tool_name},
                }
            else:
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

    def _chat_completion_tools(
        self,
        *,
        context_budget: Optional[ContextBudget] = None,
        required_tool_name: Optional[str] = None,
        user_input: Optional[str] = None,
    ) -> List[Any]:
        if not self.enable_tools:
            return []
        if len(self._tool_registry) <= 0:
            return []
        tools = self._tool_registry.get_all()
        if required_tool_name:
            if not any(
                str(getattr(tool_def, "name", "")) == required_tool_name
                for tool_def in tools
            ):
                logger.warning(
                    "[OpenAICompatibleLocalClient] Required native tool is unavailable: %s",
                    required_tool_name,
                )

        budget = context_budget or self._current_context_budget
        if not budget or not self._is_constrained_context_budget(budget):
            return tools

        allowed_names = self._constrained_native_tool_names(user_input)
        if not allowed_names:
            return []
        selected = [
            tool_def
            for tool_def in tools
            if str(getattr(tool_def, "name", "")) in allowed_names
        ]
        if required_tool_name and not any(
            str(getattr(tool_def, "name", "")) == required_tool_name
            for tool_def in selected
        ):
            selected = [
                tool_def
                for tool_def in tools
                if str(getattr(tool_def, "name", "")) == required_tool_name
            ] + selected
        return self._fit_native_tools_to_schema_budget(selected, budget)

    def _constrained_native_tool_names(
        self,
        user_input: Optional[str],
    ) -> set[str]:
        text = str(user_input or "")
        if not text.strip():
            return set()

        names: set[str] = set()
        if command_capability_active(text, "web_search"):
            names.add("web_search")
        if command_capability_active(text, "project_progress_review"):
            names.update(DIRECT_PROJECT_TOOL_HINT_NAMES)
        if command_capability_active(text, "project_db_update"):
            names.update(project_management_required_mutation_tools(text))
            names.update(
                {
                    "get_project_context",
                    "list_project_information",
                    "list_record_tables",
                }
            )
        if command_capability_active(text, "task_update"):
            names.update({"list_tasks", "create_task", "update_task"})
        if command_capability_active(text, "wbs_sync"):
            names.update({"sync_wbs_tasks", "get_upcoming_wbs_tasks", "list_tasks"})

        if looks_like_search_request(text):
            names.update(DIRECT_SEARCH_TOOL_HINT_NAMES)
        if looks_like_filesystem_request(text):
            names.update(DIRECT_FILESYSTEM_TOOL_HINT_NAMES)
        if (
            looks_like_project_management_request(text)
            or project_progress_review_active(text)
        ):
            names.update(DIRECT_PROJECT_TOOL_HINT_NAMES)
            names.update(project_management_required_mutation_tools(text))
        if looks_like_utility_request(text):
            names.add("utility_assistant")
        if looks_like_media_request(text):
            names.add("media_assistant")
        return names

    def _fit_native_tools_to_schema_budget(
        self,
        tools: List[Any],
        context_budget: ContextBudget,
    ) -> List[Any]:
        if len(tools) <= 1:
            return tools
        limit = min(
            CONSTRAINED_NATIVE_TOOL_SCHEMA_CHAR_BUDGET,
            max(3500, int(context_budget.message_budget_chars * 0.9)),
        )
        selected: List[Any] = []
        selected_specs: list[Dict[str, Any]] = []
        for tool_def in tools:
            spec = OpenAIAPIAdapter.convert(tool_def)
            candidate_specs = [*selected_specs, spec]
            candidate_size = len(
                json.dumps(
                    candidate_specs,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if candidate_size <= limit or not selected:
                selected.append(tool_def)
                selected_specs.append(spec)
        if len(selected) < len(tools):
            logger.warning(
                "[OpenAICompatibleLocalClient] Pruned native tool schemas for constrained local context: %s -> %s",
                len(tools),
                len(selected),
            )
        return selected

    def _mode_extra_body_from_kwargs(
        self, api_kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return {}
        template_kwargs = extra_body.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            return {}
        if "enable_thinking" not in template_kwargs:
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": template_kwargs["enable_thinking"],
            }
        }

    def _compatibility_retry_kwargs(
        self, api_kwargs: Dict[str, Any]
    ) -> tuple[Dict[str, Any], list[str]]:
        retry_kwargs = dict(api_kwargs)
        removed = []
        for key in ("tools", "tool_choice", "response_format"):
            if key in retry_kwargs:
                retry_kwargs.pop(key, None)
                removed.append(key)
        if "extra_body" in retry_kwargs:
            mode_extra_body = self._mode_extra_body_from_kwargs(api_kwargs)
            if mode_extra_body:
                if retry_kwargs["extra_body"] != mode_extra_body:
                    retry_kwargs["extra_body"] = mode_extra_body
                    removed.append("extra_body(non-mode)")
            else:
                retry_kwargs.pop("extra_body", None)
                removed.append("extra_body")
        return retry_kwargs, removed

    def _create_completion_with_fallback(self, api_kwargs: Dict[str, Any]) -> Any:
        try:
            return self.client.chat.completions.create(**api_kwargs)
        except Exception as exc:
            if _is_context_overflow_error(exc) and "tools" in api_kwargs:
                retry_kwargs = dict(api_kwargs)
                retry_kwargs.pop("tools", None)
                retry_kwargs.pop("tool_choice", None)
                logger.warning(
                    "[OpenAICompatibleLocalClient] Context overflow with native tools; retrying without tools: %s",
                    exc,
                )
                return self.client.chat.completions.create(**retry_kwargs)
            if _is_local_model_loading_error(exc) or _is_context_overflow_error(exc):
                raise
            retry_kwargs, removed = self._compatibility_retry_kwargs(api_kwargs)
            if not removed:
                raise
            logger.warning(
                "[OpenAICompatibleLocalClient] Compatibility retry without %s: %s",
                ", ".join(removed),
                exc,
            )
            return self.client.chat.completions.create(**retry_kwargs)

    def _message_content(self, message: Any) -> str:
        content = _message_field(message, "content")
        if not isinstance(content, str):
            return ""
        return _strip_leading_think_markup(content)

    def _chat_template_thinking_value(self, api_kwargs: Dict[str, Any]) -> Any:
        extra_body = api_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            return None
        template_kwargs = extra_body.get("chat_template_kwargs")
        if not isinstance(template_kwargs, dict):
            return None
        return template_kwargs.get("enable_thinking")

    def _should_retry_without_thinking(
        self, message: Any, api_kwargs: Dict[str, Any]
    ) -> bool:
        if self._chat_template_thinking_value(api_kwargs) is False:
            return False
        if _message_has_reasoning_output(message):
            return True
        if _is_qwopus_model(self.model_name):
            return self._chat_template_thinking_value(api_kwargs) is True
        return False

    def _create_completion_retrying_empty_reasoning(
        self, api_kwargs: Dict[str, Any], *, reason: str
    ) -> Any:
        response = self._create_completion_with_fallback(api_kwargs)
        choice = response.choices[0]
        message = choice.message
        if getattr(message, "tool_calls", None) or self._message_content(message):
            return response
        if not self._should_retry_without_thinking(message, api_kwargs):
            return response
        logger.warning(
            "[OpenAICompatibleLocalClient] Empty content with reasoning output; "
            "retrying with thinking disabled (%s)",
            reason,
        )
        retry_kwargs = self._set_chat_template_thinking(api_kwargs, False)
        api_kwargs.clear()
        api_kwargs.update(retry_kwargs)
        return self._create_completion_with_fallback(api_kwargs)

    def _empty_response_fallback(self) -> str:
        return LOCAL_MODEL_EMPTY_RESPONSE

    def _tool_loop_completion(self, api_kwargs: Dict[str, Any]) -> Any:
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="tool follow-up",
        )
        self._capture_generation_metrics(response)
        return response

    def _tool_result_retry_input(self, user_input: str, tool_calls: list[Any]) -> str:
        if not tool_calls:
            return user_input
        lines = [
            "Confirmed tool results from the previous attempt:",
            "Use these results as evidence and do not claim any other tool execution.",
        ]
        for record in tool_calls[-6:]:
            tool_name = str(getattr(record, "tool", "") or "tool")
            arguments = getattr(record, "arguments", {}) or {}
            result = clip_text(str(getattr(record, "result", "") or ""), 1600)
            try:
                arguments_text = json.dumps(arguments, ensure_ascii=False)
            except TypeError:
                arguments_text = str(arguments)
            lines.append(f"- {tool_name}({clip_text(arguments_text, 500)}): {result}")
        lines.append("")
        lines.append("Current user request:")
        lines.append(user_input)
        return "\n".join(lines)

    def _plain_empty_response_retry(
        self,
        user_input: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: Optional[ContextBudget],
        reason: str,
        executed_tool_calls: Optional[list[Any]] = None,
    ) -> str:
        tool_calls = executed_tool_calls or []
        budget = context_budget or self._context_budget(max_tokens)
        retry_input = self._tool_result_retry_input(user_input, tool_calls)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Answer the user's latest "
                    "message directly in Japanese when the user writes Japanese."
                    f"\n\n{_current_date_context()}"
                ),
            },
            {
                "role": "user",
                "content": clip_text_preserve_tail(
                    retry_input,
                    max(1000, budget.message_budget_chars - 1200),
                ),
            },
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=budget,
            tools_enabled=False,
        )
        api_kwargs, _ = self._compatibility_retry_kwargs(api_kwargs)
        logger.warning(
            "[OpenAICompatibleLocalClient] Retrying empty local response with minimal prompt (%s)",
            reason,
        )
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason=f"minimal retry after {reason}",
        )
        self._capture_generation_metrics(response)
        message = response.choices[0].message
        content = self._message_content(message)
        if content:
            return guard_tool_execution_claims(content, tool_calls)
        if "extra_body" in api_kwargs:
            retry_kwargs = dict(api_kwargs)
            retry_kwargs.pop("extra_body", None)
            logger.warning(
                "[OpenAICompatibleLocalClient] Minimal empty-response retry returned empty; retrying without extra_body (%s)",
                reason,
            )
            response = self._create_completion_with_fallback(retry_kwargs)
            message = response.choices[0].message
            content = self._message_content(message)
            if content:
                return guard_tool_execution_claims(content, tool_calls)
        return ""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools_enabled: bool = True,
        context_budget: Optional[ContextBudget] = None,
        fallback_user_input: Optional[str] = None,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
    ) -> str:
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=context_budget,
            tools_enabled=tools_enabled,
            required_tool_name=required_tool_name,
            native_tool_user_input=native_tool_user_input,
        )
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="chat completion",
        )
        self._capture_generation_metrics(response)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            return self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
            )
        content = self._message_content(choice.message)
        if fallback_user_input and project_progress_review_active(fallback_user_input):
            return self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
            )
        if required_tool_name:
            logger.warning(
                "[OpenAICompatibleLocalClient] required tool %s was not called",
                required_tool_name,
            )
            return (
                "ツール実行の検証に失敗しました: "
                f"必須ツール `{required_tool_name}` が実行されませんでした。"
            )
        if content:
            return guard_tool_execution_claims(content, [])
        logger.warning(
            "[OpenAICompatibleLocalClient] local model returned empty content"
        )
        if fallback_user_input:
            retry_content = self._plain_empty_response_retry(
                fallback_user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="chat completion",
            )
            if retry_content:
                return retry_content
        return self._empty_response_fallback()

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        stream_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=self._current_context_budget,
        )
        stream_kwargs["stream"] = True
        stream_kwargs.pop("tools", None)
        stream_kwargs.pop("tool_choice", None)
        stream_kwargs.pop("response_format", None)
        stream_kwargs = self._with_stream_safe_extra_body(stream_kwargs)
        stream = self.client.chat.completions.create(**stream_kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            else:
                self._capture_generation_metrics(chunk)

    def _handle_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        assistant_message: Any,
        api_kwargs: Dict[str, Any],
        context_budget: ContextBudget,
        *,
        temperature: float,
        max_tokens: Optional[int],
        fallback_user_input: Optional[str] = None,
        max_rounds: int = 5,
    ) -> str:
        effective_max_rounds = max(
            max_rounds,
            agentic_max_rounds(self, fallback_user_input),
        )
        result = run_openai_tool_call_loop(
            initial_messages=messages,
            assistant_message=assistant_message,
            api_kwargs=api_kwargs,
            registry=self._tool_registry,
            create_completion=self._tool_loop_completion,
            log_prefix="OpenAICompatibleLocalClient",
            max_rounds=effective_max_rounds,
            return_result=True,
            max_tool_result_chars=context_budget.tool_result_chars,
            message_content=self._message_content,
            config=self.config,
            user_input=fallback_user_input,
            require_project_context_read=self._requires_project_context_read(
                fallback_user_input,
            ),
        )
        self._last_tool_calls.extend(result.tool_calls)
        if result.final_output:
            return guard_tool_execution_claims(result.final_output, result.tool_calls)
        logger.warning(
            "[OpenAICompatibleLocalClient] tool call loop returned empty content"
        )
        if fallback_user_input:
            retry_user_input = self._tool_result_retry_input(
                fallback_user_input,
                result.tool_calls,
            )
            retry_content = self._plain_empty_response_retry(
                retry_user_input,
                temperature=temperature,
                max_tokens=max_tokens,
                context_budget=context_budget,
                reason="tool call loop",
                executed_tool_calls=result.tool_calls,
            )
            if retry_content:
                return retry_content
        return self._empty_response_fallback()

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: ContextBudget,
        user_input: str,
    ) -> str:
        review_messages = [
            {
                "role": "system",
                "content": (
                    "You are a careful AoiTalk agent. Follow the user's verifier "
                    "or continuation instruction exactly."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        return self.chat(
            review_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools_enabled=True,
            context_budget=context_budget,
            fallback_user_input=user_input,
            native_tool_user_input=user_input,
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
        self._last_generation_metrics = None
        self._last_tool_calls = []
        self._last_agentic_events = []
        project_token = None
        specialist_provider_token = set_runtime_specialist_provider(
            "openai_compatible_local"
        )
        tool_policy_token = set_current_user_input(user_input)
        policy = get_client_generation_policy(self)
        generation_policy_token = set_current_generation_policy(policy)
        context_budget: Optional[ContextBudget] = None
        project_context: Optional[dict[str, Any]] = None
        try:
            context_budget = self._context_budget(max_tokens)
            self._current_context_budget = context_budget
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            messages, tool_hint_context = self._build_model_messages_for_budget(
                user_input,
                project_context,
                context_budget,
            )
            required_tool_name = self._required_tool_name(user_input)
            if stream:
                return self._stream_response(messages, temperature, max_tokens, user_input)
            response_text = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools_enabled=True,
                context_budget=context_budget,
                fallback_user_input=user_input,
                required_tool_name=required_tool_name,
                native_tool_user_input=user_input,
            )

            def _agentic_event_callback(event_type: str, data: Dict[str, Any]) -> None:
                event_data = dict(data or {})
                event_data["event_type"] = event_type
                self._last_agentic_events.append(event_data)
                if stream_callback:
                    self._run_async_sync(stream_callback(event_type, data))

            response_text = run_agentic_completion_loop_sync(
                client=self,
                run_once=lambda prompt: self._run_agentic_review_once(
                    prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    context_budget=context_budget,
                    user_input=user_input,
                ),
                context=render_messages_for_review(messages),
                user_input=user_input,
                initial_response=response_text,
                event_callback=_agentic_event_callback,
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
            elif _is_context_overflow_error(exc):
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model context overflow: %s",
                    exc,
                )
                if not stream:
                    try:
                        retry_text = self._retry_after_context_overflow(
                            user_input=user_input,
                            project_context=project_context,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            required_tool_name=required_tool_name,
                        )
                        if retry_text:
                            self.history_manager.add_message("user", user_input)
                            self.history_manager.add_message("assistant", retry_text)
                            return retry_text
                    except Exception as retry_exc:
                        logger.warning(
                            "[OpenAICompatibleLocalClient] compact overflow retry failed: %s",
                            retry_exc,
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
            reset_runtime_specialist_provider(specialist_provider_token)
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            self._current_context_bundle = None
            self._current_context_budget = None
            self._current_tool_hint_context = ""

    def generate_title(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 64,
    ) -> str:
        """Generate a session title without normal chat context or history."""
        self._last_generation_metrics = None
        context_budget = self._context_budget(max_tokens)
        messages = [
            {"role": "system", "content": TITLE_GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature,
            max_tokens,
            context_budget=context_budget,
            tools_enabled=False,
        )
        api_kwargs.pop("response_format", None)
        if _is_qwopus_model(self.model_name):
            api_kwargs = self._set_chat_template_thinking(api_kwargs, False)
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="title generation",
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

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
            elif _is_context_overflow_error(exc):
                self._reduce_context_budget_after_overflow()
                logger.warning(
                    "[OpenAICompatibleLocalClient] local model context overflow during streaming: %s",
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
        if _is_context_overflow_error(exc):
            return self._get_context_overflow_fallback_response()
        return self._get_fallback_response()

    def _reduce_context_budget_after_overflow(self) -> None:
        budget = self._current_context_budget or self._context_budget()
        reduced = reduced_context_window_after_overflow(budget.context_window_tokens)
        if (
            self._context_window_override_tokens is None
            or reduced < self._context_window_override_tokens
        ):
            self._context_window_override_tokens = reduced
            logger.warning(
                "[OpenAICompatibleLocalClient] Reduced local context budget after overflow: %s -> %s tokens",
                budget.context_window_tokens,
                reduced,
            )

    def _get_context_overflow_fallback_response(self) -> str:
        parts = [LOCAL_MODEL_CONTEXT_OVERFLOW_RESPONSE]
        budget = self._current_context_budget or self._context_budget()
        evidence_blocks: list[str] = []
        if self._current_tool_hint_context:
            evidence_blocks.append(self._current_tool_hint_context)
        if self._current_context_bundle:
            evidence_blocks.extend(
                [
                    self._current_context_bundle.project_context_block,
                    self._current_context_bundle.project_information_block,
                    self._current_context_bundle.project_pack_block,
                ]
            )
        evidence = "\n\n".join(block.strip() for block in evidence_blocks if block and block.strip())
        if evidence:
            parts.append(clip_text(evidence, budget.context_bundle_chars))
        else:
            parts.append(
                "取得済みの案件情報DB・ツール実行結果・会話根拠はありませんでした。"
                "メッセージを短くするか、ローカルLLMサーバーのコンテキスト設定を確認してください。"
            )
        return "\n\n".join(parts)

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

    async def generate_title_async(self, prompt: str) -> str:
        return await asyncio.to_thread(self.generate_title, prompt)

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
    model = (
        response_model
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_MODEL")
        or _config_get(config, "llm_model")
        or _config_get(config, "openai_compatible_local.model")
        or DEFAULT_LOCAL_MODEL
    )
    base_url = openai_compatible_local_base_url(config, model=model)
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

