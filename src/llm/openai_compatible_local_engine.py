"""Generic local OpenAI-compatible LLM client.

This provider is for already-running local servers such as llama.cpp
llama-server. Optional OpenAI-compatible features such as native tool calling,
response_format, and user-configured extra_body are opt-in compatibility
parameters; mode-specific extra_body is applied automatically where needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Union

from openai import OpenAI

from ..config import Config
from ..memory.history import HistoryManager
from ..services.project_context import (
    ProjectContextResolver,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from ..services.turn_context import get_turn_context
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..services.story_chat_context import run_story_chat_context_sync
from ..services.outbound_privacy_service import OutboundPrivacyGateway
from ..tools.adapters import OpenAIAPIAdapter
from ..tools.registry import ToolRegistry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    GenerationProfile,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_cancellation import GenerationInterrupted
from .generation_error import GenerationErrorKind, classify_generation_error
from .agentic_completion import (
    agentic_max_rounds,
    format_tool_execution_evidence,
    render_messages_for_review,
    run_agentic_completion_loop_sync,
    successful_empty_task_search,
    tool_loop_completion_confirmed,
)
from .agent_runtime import (
    DIRECT_PROJECT_TOOL_HINT_NAMES,
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
    llama_cpp_model_profile,
    llama_cpp_profile_capabilities,
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_reasoning_effort_request_extra_body,
    openai_compatible_local_base_url,
    openai_compatible_server_profile,
)
from .conversation_context import (
    PromptMessages,
    build_prompt_messages,
    normalize_usage,
    persist_usage_sync,
    stable_cache_key,
)
from .context_snapshot import component, context_bundle_components, message_components, reconcile_snapshot, snapshot, tool_components, without_text
from .multimodal import openai_content_parts
from .prompts import build_unified_instructions
from .provider_capabilities import ProviderCapabilities
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import (
    LOAD_TOOL_PACK_TOOL_NAME,
    auto_load_packs_for_tool_names,
    ensure_load_tool_pack_tool,
    tool_pack_session_for_client,
)
from .tool_exposure import (
    filter_tools_for_client,
    filtered_registry_for_client,
)
from .turn_stream_events import (
    SyncStreamEmitter,
    bind_stream_callback_loop,
    emit_thinking,
    make_sync_stream_emitter,
    thinking_text_from_message,
)
from .tool_policy import (
    PROJECT_COMMAND_CAPABILITIES,
    command_capability_active,
    command_capabilities_from_text,
    mutation_execution_forbidden,
    looks_like_managed_workspace_request,
    looks_like_project_management_request,
    project_management_required_mutation_tools,
    project_progress_review_active,
    reset_current_user_input,
    sanitize_command_capabilities,
    set_current_user_input,
)

# A constrained native schema must expose the same safe, high-level set for
# an explicit ``workspace_file_operation`` command and for a server-verified
# Project attachment.  Keep this set deliberately narrower than every legacy
# filesystem tool (no native shell/repository mutation tools).
MANAGED_WORKSPACE_NATIVE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_workspace_tree",
        "list_directory",
        "read_file",
        "create_workspace_directory",
        "copy_workspace_item",
        "move_workspace_item",
        "docs_read",
        "docs_attach_workspace_file",
        "docs_place_workspace_file",
    }
)

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL
DEFAULT_LOCAL_MODEL = "local-model"
DEFAULT_LOCAL_API_KEY = "dummy"
CONSTRAINED_NATIVE_TOOL_SCHEMA_CHAR_BUDGET = 9000
LOCAL_MODEL_LOADING_RESPONSE = (
    "ローカルLLMはモデルを読み込み中です。少し待ってからもう一度送信してください。"
)
LOCAL_MODEL_SERVER_START_FAILED_RESPONSE = (
    "ローカルLLMサーバー（llama-server）を起動できませんでした。"
    "logs/models/llama_cpp.log と、選択したprofileのruntime要件を確認してから再試行してください。"
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


def _is_connection_error(exc: Exception) -> bool:
    return classify_generation_error(exc).kind == GenerationErrorKind.CONNECTION


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


def _llama_cpp_profile_capability(
    profile: Optional[Dict[str, Any]],
    capability: str,
) -> Optional[bool]:
    """Return an explicitly declared capability from a runtime profile.

    ``None`` means the profile is unknown or does not declare the capability;
    callers must preserve the generic external local-model behaviour in that
    case.  Only an explicit ``False`` disables a legacy opt-in feature.
    """

    capabilities = llama_cpp_profile_capabilities(profile=profile)
    if not isinstance(capabilities, dict) or capability not in capabilities:
        return None
    return bool(capabilities.get(capability))


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
        self._privacy_gateway = OutboundPrivacyGateway(config)
        self.base_url = _normalize_base_url(base_url)
        self.model_name = model or DEFAULT_LOCAL_MODEL
        self.api_key = api_key or DEFAULT_LOCAL_API_KEY
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self._llama_cpp_profile = llama_cpp_model_profile(self.model_name)
        profile_tools = _llama_cpp_profile_capability(
            self._llama_cpp_profile,
            "tools",
        )
        profile_reasoning = _llama_cpp_profile_capability(
            self._llama_cpp_profile,
            "reasoning",
        )
        # A profile-declared limitation wins over the provider's opt-in
        # setting.  Unknown profiles (including ``local-model``) keep the
        # historical config-controlled behaviour.
        self.enable_tools = bool(enable_tools) and profile_tools is not False
        self.enable_response_format = bool(enable_response_format)
        self.enable_extra_body = bool(enable_extra_body)
        self.extra_body = extra_body if isinstance(extra_body, dict) else {}
        self.server_profile = openai_compatible_server_profile(
            config, base_url=self.base_url
        )
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=profile_tools is not False,
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
        self._last_context_snapshots: List[Dict[str, Any]] = []
        self._last_tool_calls: List[Any] = []
        self._last_agentic_events: List[Dict[str, Any]] = []
        self._last_model_transcript: List[Dict[str, Any]] = []
        self._last_usage: Dict[str, Any] = {}
        self._last_tool_loop_messages: List[Dict[str, Any]] = []
        self._last_tool_loop_completion_confirmed = False
        self._last_audit_tool_calls: List[Any] = []
        self._current_turn_system_content = ""
        self._last_local_server_ensure_error: Optional[str] = None
        self._llama_cpp_generation_lease_tickets: set[Any] = set()
        self._runtime_unavailable_reason: Optional[str] = None
        self._runtime_unavailable_at: float = 0.0
        # Managed profiles use the profile-owned reasoning contract.  A known
        # profile that explicitly disables reasoning has no generic
        # fast/thinking mode; unknown external endpoints retain legacy fast.
        self._profile_reasoning_supported = profile_reasoning
        self._current_llm_mode = self._effective_reasoning_effort() or (
            "" if profile_reasoning is False else "fast"
        )
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

    def _get_memory_metadata(self) -> Dict[str, Any]:
        return self.session_metadata.copy() if self.session_metadata else {}

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        if self._last_generation_metrics:
            metadata["generation_metrics"] = dict(self._last_generation_metrics)
        if self._last_context_snapshots:
            latest = dict(self._last_context_snapshots[-1])
            latest["requests"] = [dict(item) for item in self._last_context_snapshots[-8:]]
            metadata["context_snapshot"] = latest
        if self._last_model_transcript:
            metadata["model_transcript"] = [dict(item) for item in self._last_model_transcript]
        metadata["cache_usage"] = dict(self._last_usage)
        metadata["cache_diagnostics"] = {
            "provider": "openai_compatible_local",
            "model": self.model_name,
            "server_profile": self.server_profile.get("name"),
            "cache_mode": self.server_profile.get("cache_mode"),
            "cache_supported": self.server_profile.get("cache_supported"),
            "cache_active": self._last_usage.get("cache_active"),
            "cache_configured": self.server_profile.get("cache_supported") is True,
            "metrics_source": self.server_profile.get("metrics_source"),
            "cache_key": getattr(self, "_cache_key", None),
        }
        return metadata

    def _capture_context_request(self, api_kwargs: Dict[str, Any], *, reason: str) -> None:
        budget = self._current_context_budget
        rendered_bundle, bundle_parts = context_bundle_components(self._current_context_bundle)
        parts = [
            *message_components(without_text(api_kwargs.get("messages", []), rendered_bundle)),
            *bundle_parts,
            *tool_components(api_kwargs.get("tools", []), source="chat.completions tools payload"),
        ]
        active_names = {
            str(((item.get("function") or {}).get("name") or ""))
            for item in api_kwargs.get("tools", []) if isinstance(item, dict)
        }
        for tool_def in self._tool_registry.get_all():
            name = str(getattr(tool_def, "name", "") or "")
            if name and name not in active_names:
                parts.append(component(
                    "native_tool_schemas", "Native tool schemas", source="runtime tool registry",
                    status="deferred", tokens=0, preview=f"{name}（未送信）",
                ))
        item = snapshot(
            provider="openai_compatible_local",
            model=self.model_name,
            components=parts,
            context_window_tokens=budget.context_window_tokens if budget else None,
            response_tokens=int(api_kwargs.get("max_tokens") or (budget.response_tokens if budget else 0)),
            request_index=len(self._last_context_snapshots),
            request_kind=reason,
            window_source=budget.source if budget else None,
        )
        self._last_context_snapshots.append(item)
        self._last_context_snapshots = self._last_context_snapshots[-8:]

    def _capture_generation_metrics(self, response: Any) -> Dict[str, Any]:
        payload = _as_plain_dict(response)
        timings = _as_plain_dict(payload.get("timings"))
        usage = _as_plain_dict(payload.get("usage"))
        normalized_usage = normalize_usage(
            usage,
            provider="openai_compatible_local",
            resolved_model=(
                str(payload.get("model")) if payload.get("model") else None
            ),
        )
        self._last_usage = {
            key: value for key, value in normalized_usage.items() if value is not None
        }

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
            return dict(self._last_usage)

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
        if prompt_tokens is not None and self._last_context_snapshots:
            self._last_context_snapshots[-1] = reconcile_snapshot(
                self._last_context_snapshots[-1], prompt_tokens
            )
        return dict(self._last_usage)

    def _capture_and_persist_usage(
        self,
        response: Any,
        *,
        request_type: str = "chat",
        latency_ms: int = 0,
        is_streaming: bool = False,
    ) -> Dict[str, Any]:
        """Capture and persist one successful local completion response."""

        usage = self._capture_generation_metrics(response)
        if usage:
            persist_usage_sync(
                self,
                provider="openai_compatible_local",
                model=self.model_name,
                usage=usage,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )
        return usage

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
                include_static_tool_reference=False,
                custom_instructions=custom_instructions,
                # This client uses OpenAI-compatible function tool calls (or
                # a tool-free completion), never the CLI textual protocol.
                tool_protocol="native",
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

    def _reasoning_effort_metadata(self) -> Optional[Dict[str, Any]]:
        if _llama_cpp_profile_capability(
            getattr(self, "_llama_cpp_profile", None),
            "reasoning",
        ) is False:
            return None
        return llama_cpp_reasoning_effort_metadata(self.model_name)

    def _configured_reasoning_effort(self) -> Optional[str]:
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return None
        values = (
            _config_get(self.config, "runtime.target_reasoning_effort"),
            _config_get(
                self.config,
                "openai_compatible_local.llama_cpp.reasoning_effort",
            ),
            _config_get(self.config, "openai_compatible_local.reasoning_effort"),
        )
        for value in values:
            normalized = str(value or "").strip().lower()
            if normalized in metadata["options"]:
                return normalized
        return None

    def _effective_reasoning_effort(self) -> Optional[str]:
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return None
        configured = self._configured_reasoning_effort()
        return configured or str(metadata["default"])

    def set_llm_mode(self, mode: str) -> None:
        metadata = self._reasoning_effort_metadata()
        if metadata:
            normalized = str(mode or "").strip().lower()
            if normalized not in metadata["options"]:
                raise ValueError(
                    "Unsupported reasoning effort for managed local profile: "
                    f"{mode!r}; expected one of {metadata['options']}"
                )
            self._current_llm_mode = normalized
            return
        if getattr(self, "_profile_reasoning_supported", None) is False:
            # Do not persist or expose a synthetic fast/thinking value for a
            # known profile whose registry explicitly disables reasoning.
            self._current_llm_mode = ""
            return
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
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )
        self.system_prompt = self._build_system_prompt()

    def _include_project_context_enabled(self) -> bool:
        return project_context_enabled_for_client(self)

    def _context_bundle_for_turn(
        self,
        bundle: Optional[ContextBundle] = None,
    ) -> Optional[ContextBundle]:
        """Hide stale selected-Project layers when Project Context is OFF.

        The normal ContextBuilder call already receives the immutable turn
        flag, but direct-client fallbacks and test doubles may hand this
        provider a previously-built bundle.  Never let that stale bundle
        re-introduce the selected project's scope into a turn that explicitly
        disabled Project Context; user/session memory remains available.
        """

        current = (
            getattr(self, "_current_context_bundle", None)
            if bundle is None
            else bundle
        )
        if current is None or self._include_project_context_enabled():
            return current
        return dataclasses.replace(
            current,
            project_context_block="",
            project_information_block="",
            agent_memory_block="",
            project_pack_block="",
            task_context_block="",
        )

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
        system_prompt = self._system_prompt_for_budget(context_budget)
        context_window = self.history_manager.context_window_size
        history_limit = min(context_window * 2, context_budget.history_messages)
        history = self.history_manager.get_model_messages()[-history_limit:]
        messages = [
            {"role": "system", "content": system_prompt},
            *build_prompt_messages(
                history,
                summary=self.history_manager.summary,
                current_user_input=clip_text_preserve_tail(
                    user_input, context_budget.message_budget_chars
                ),
            ),
        ]
        messages = [
            {
                **message,
                "content": (
                    clip_text(str(message.get("content") or ""), context_budget.history_message_chars)
                    if message.get("role") != "user" or message is not messages[-1]
                    else message.get("content")
                ),
            }
            for message in messages
        ]
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
                "Docs are AoiTalk's KnowledgeNode outline, Projects are case scopes, and Workspaces store files; keep these concepts separate.",
                "When Project Context is OFF, the Selected Project is only weak UI state, not the request target; inspect Project information only when the request requires it.",
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
        bundle = context_bundle
        if self.history_manager.summary and getattr(bundle, "session_context_block", ""):
            bundle = dataclasses.replace(bundle, session_context_block="")
        return bundle.render_for_prompt(context_budget.context_bundle_chars)

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
        # Keep direct/fallback ContextBuilder implementations subject to the
        # immutable Project Context OFF boundary as well.
        context_bundle = self._context_bundle_for_turn()
        tool_hint_context = self._build_tool_hint_context(
            user_input,
            context_budget,
        )
        self._current_tool_hint_context = tool_hint_context
        model_user_input = compose_tool_hint_user_message(
            user_input,
            "",
        )
        context_block = self._context_block_for_budget(
            context_bundle,
            context_budget,
            has_tool_hints=bool(tool_hint_context),
        )
        dynamic_context: list[tuple[str, str]] = []
        if context_block:
            dynamic_context.append(("Current context bundle", context_block))
        if tool_hint_context:
            dynamic_context.append(("Current tool hints", tool_hint_context))
        model_messages = build_prompt_messages(
            self.history_manager.get_model_messages()[-context_budget.history_messages :],
            summary=self.history_manager.summary,
            current_user_input=clip_text_preserve_tail(
                model_user_input, context_budget.message_budget_chars
            ),
            dynamic_context=dynamic_context,
        )
        # Keep the stable system prefix independent from the current date and
        # current-turn retrieval/tool hints.
        messages = [
            {
                "role": "system",
                "content": (
                    self._system_prompt_for_budget(context_budget)
                    + "\n\n"
                    + _current_date_context()
                ),
            },
            *model_messages,
        ]
        return (
            self._fit_messages_to_context_budget(messages, context_budget),
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
                    request_type="retry",
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

    def _get_story_chat_context_sync(self):
        """Resolve the trusted StoryWritingSession for this conversation."""
        # Lightweight provider instances used by review/schema paths may be
        # created with ``__new__`` before the history-session property is
        # initialized.  Read the backing field directly so that an ordinary
        # non-Story context remains ``None`` rather than becoming a resolver
        # error and failing the whole tool schema closed.
        session_id = getattr(self, "_current_session_id", None)
        if not session_id:
            return None
        return run_story_chat_context_sync(self._run_async_sync, str(session_id))

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
            registry=filtered_registry_for_client(self, self._tool_registry),
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

        # The UI/controller capability is authority to perform a requested
        # mutation, not authority to override an explicit user prohibition.
        # Keep the turn read-only even when task_update/project_db_update was
        # carried on the client from the trusted command surface.
        if mutation_execution_forbidden(user_input):
            return None

        explicit_capabilities = set(
            getattr(self, "current_command_capabilities", ()) or ()
        ) | command_capabilities_from_text(user_input)
        if not explicit_capabilities.intersection(PROJECT_COMMAND_CAPABILITIES):
            # Natural-language task/DB/WBS wording is not proof that a
            # particular mutation is required.  Let the model choose the
            # appropriate direct tool instead of setting tool_choice to a
            # fixed function.
            return None

        required_tools: set[str] = set(
            project_management_required_mutation_tools(user_input)
        )
        # Terminal/voice callers also carry capabilities on the client while
        # passing the raw user text.  Preserve explicit command semantics even
        # when the trusted preamble is not present in ``user_input``.
        if "project_db_update" in explicit_capabilities:
            required_tools.update(
                {
                    "organize_project_information_from_folder",
                    "patch_project_information_doc",
                    "attach_project_information_reference",
                }
            )
        if "task_update" in explicit_capabilities:
            required_tools.update({"create_task", "update_task"})
        if "wbs_sync" in explicit_capabilities:
            required_tools.add("sync_wbs_tasks")
        # 強制するツールは実効集合（コア + ロード済み pack）の中から選ぶ。
        exposed = filtered_registry_for_client(self, self._tool_registry)
        for tool_name in (
            "organize_project_information_from_folder",
            "sync_wbs_tasks",
            "sync_issue_table",
            "patch_project_information_doc",
            "create_record_table",
            "create_task",
            "update_task",
        ):
            if tool_name in required_tools and tool_name in exposed:
                return tool_name
        return None

    def _requires_project_context_read(self, user_input: str | None) -> bool:
        return (
            project_context_enabled_for_client(self, default=False)
            and bool(self.current_project_id or self.current_session_id)
            and looks_like_project_management_request(str(user_input or ""))
        )

    def _is_managed_llama_cpp_runtime(self) -> bool:
        """True when AoiTalk may launch or relaunch an owned llama-server."""

        return self._can_attempt_managed_llama_cpp_recovery()

    def _can_attempt_managed_llama_cpp_recovery(self) -> bool:
        if not self.config:
            return False
        try:
            from src.service_manager import llama_cpp_managed_launch_configured

            return llama_cpp_managed_launch_configured(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return False

    def _managed_llama_cpp_launch_configuration_error(self) -> Optional[str]:
        if not self.config:
            return None
        try:
            from src.service_manager import llama_cpp_managed_launch_configuration_error

            return llama_cpp_managed_launch_configuration_error(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return None

    def _is_manual_managed_llama_cpp_runtime(self) -> bool:
        if not self.config:
            return False
        try:
            from src.service_manager import llama_cpp_manual_managed_runtime

            return llama_cpp_manual_managed_runtime(
                self.config,
                model=self.model_name,
            )
        except Exception:
            return False

    def _mark_runtime_unavailable(self, reason: str) -> None:
        detail = str(reason or "").strip()
        if not detail:
            return
        self._runtime_unavailable_reason = detail
        self._runtime_unavailable_at = time.monotonic()

    def _clear_runtime_unavailability(self) -> None:
        self._runtime_unavailable_reason = None
        self._runtime_unavailable_at = 0.0

    def is_runtime_known_unavailable(self) -> bool:
        return bool(str(self._runtime_unavailable_reason or "").strip())

    def _uses_llama_cpp_tool_choice_transport(self) -> bool:
        if self.server_profile.get("name") == "llama.cpp":
            return True
        if llama_cpp_model_profile(self.model_name) is not None:
            return True
        if not self.config:
            return False
        try:
            from src.service_manager._local_llm_servers import (
                _llama_cpp_selected_model,
                _llama_cpp_settings,
            )

            selected_model = _llama_cpp_selected_model(self.config, self.model_name)
            settings = _llama_cpp_settings(self.config, model=selected_model)
            return bool(str(settings.get("model_path") or "").strip())
        except Exception:
            return False

    def _is_llama_cpp_server(self) -> bool:
        return self._uses_llama_cpp_tool_choice_transport()

    def _format_last_tool_calls_evidence(self) -> str:
        return format_tool_execution_evidence(self._last_tool_calls)

    def _required_project_context_read_tool_name(
        self,
        user_input: str,
    ) -> Optional[str]:
        if not self._requires_project_context_read(user_input):
            return None

        # Grounding is request-local.  Once a successful project read has
        # been recorded during this turn, do not force the same read again on
        # an agentic continuation.  The audit ledger is reset at the start of
        # ``generate_response`` and deliberately excludes prior turns.
        if self._project_context_read_already_satisfied():
            return None

        # Task mutations need task identity/status/parent information as the
        # first grounding read.  Prefer ``list_tasks`` over a generic project
        # information read when it is available; the latter cannot resolve
        # ordinal task references such as "6, 7, 8".
        task_mutation_tools = {
            "create_task",
            "update_task",
            "delete_task",
            "assign_task",
            "schedule_task",
        }
        required_mutations = project_management_required_mutation_tools(user_input)
        exposed = filtered_registry_for_client(self, self._tool_registry)
        if required_mutations.intersection(task_mutation_tools) and "list_tasks" in exposed:
            return "list_tasks"

        for tool_name in project_context_required_read_tool_names(exposed):
            return tool_name
        return None

    def _project_context_read_already_satisfied(self) -> bool:
        """Return whether this request already has a successful grounding read.

        Only ``_last_audit_tool_calls`` is considered.  It is reset at the
        beginning of every ``generate_response`` request, so evidence from a
        prior turn cannot suppress the current turn's initial grounding.
        """

        required_reads = set(project_context_required_read_tool_names())
        if not required_reads:
            return False
        for record in getattr(self, "_last_audit_tool_calls", ()) or ():
            if isinstance(record, dict):
                tool_name = str(record.get("tool") or record.get("name") or "")
                explicit_success = record.get("successful", record.get("success"))
                result = record.get("result", record.get("output", ""))
            else:
                tool_name = str(
                    getattr(record, "tool", None)
                    or getattr(record, "name", None)
                    or ""
                )
                explicit_success = getattr(record, "successful", None)
                if explicit_success is None:
                    explicit_success = getattr(record, "success", None)
                result = getattr(record, "result", "")
            if tool_name not in required_reads:
                continue
            if isinstance(explicit_success, bool):
                if explicit_success:
                    return True
                continue
            # Older provider records may omit an explicit success flag.  Keep
            # the same conservative error-prefix semantics as the shared
            # record helpers rather than treating arbitrary failures as reads.
            lowered = str(result or "").strip().casefold()
            if not lowered.startswith(
                ("error:", "tool not found:", "tool execution error:")
            ):
                return True
        return False

    def _required_tool_name(self, user_input: str) -> Optional[str]:
        required = self._required_command_tool_name(
            user_input
        ) or self._required_project_context_read_tool_name(
            user_input
        ) or self._required_project_management_tool_name(user_input)
        if required:
            return required
        required_mutations = project_management_required_mutation_tools(
            user_input
        )
        if "create_task" in required_mutations and (
            successful_empty_task_search(
                getattr(self, "_last_audit_tool_calls", None)
            )
            is True
        ):
            exposed = filtered_registry_for_client(self, self._tool_registry)
            if "create_task" in exposed:
                return "create_task"
        return None

    def _build_context_bundle_sync(
        self,
        user_input: str,
        project_context: Optional[dict[str, Any]],
        context_budget: ContextBudget,
    ) -> Optional[ContextBundle]:
        # Project Context is governed by the request-scoped flag (or the
        # provider compatibility flag), never by exact natural-language
        # phrases such as "検索してね".
        include_project_context = self._include_project_context_enabled()
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
                    task_id=get_turn_context().task_id,
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
        metadata = self._reasoning_effort_metadata()
        if not metadata:
            return {}
        effort = str(self._current_llm_mode or "").strip().lower()
        if effort not in metadata["options"]:
            effort = str(metadata["default"])
        # The profile owns transport/path; malformed metadata fails closed
        # rather than silently reverting to a stale hard-coded wire shape.
        extra_body = llama_cpp_reasoning_effort_request_extra_body(
            self.model_name,
            effort,
        )
        if extra_body is None:
            raise ValueError(
                "Managed local profile has invalid reasoning effort wire metadata"
            )
        return extra_body

    def _profile_disables_thinking(self) -> bool:
        """Return whether the selected managed profile is non-reasoning.

        A profile capability of ``reasoning=false`` is stronger than generic
        local-server defaults.  Some llama.cpp Jinja templates otherwise
        leave their thinking channel implicit when no effort metadata exists,
        causing a non-reasoning chat profile such as Melody to spend its
        entire response budget on hidden planning text.
        """

        return getattr(self, "_profile_reasoning_supported", None) is False

    def _should_run_agentic_completion_review(self) -> bool:
        """Keep the generic review pass for capable profiles only.

        A known profile that advertises neither reasoning nor native tools has
        no actionable review/tool contract.  In the normal chat profile, and
        in an autonomous-work turn that has no command/tool requirement, a
        second full LLM request only makes short replies appear stuck.  Keep
        explicit assisted-work/review turns and work commands unchanged, as
        well as all unknown/Qwen profiles.
        """

        if not (
            self._profile_disables_thinking()
            and _llama_cpp_profile_capability(
                getattr(self, "_llama_cpp_profile", None),
                "tools",
            )
            is False
        ):
            return True

        profile = get_client_generation_policy(self).profile
        if profile == GenerationProfile.CHAT:
            return False
        if profile != GenerationProfile.AUTONOMOUS_WORK:
            return True

        # The chat composer can retain ``autonomous_work`` from a previous
        # setting even when the current message is an ordinary prompt.  Do
        # not pay for a review that cannot execute tools in that case, while
        # preserving review for explicit command/tool turns.
        command_capabilities = getattr(self, "current_command_capabilities", ())
        if command_capabilities:
            return True
        return getattr(self, "current_tool_required", None) is True

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

    def _with_stream_safe_extra_body(self, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return api_kwargs

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
        if required_tool_name and self._is_llama_cpp_server():
            narrowed_tools = [
                tool_def
                for tool_def in tools
                if str(getattr(tool_def, "name", "")) == required_tool_name
            ]
            if narrowed_tools:
                tools = narrowed_tools
        if tools:
            api_kwargs["tools"] = OpenAIAPIAdapter.convert_all(tools)
            available_names = {str(getattr(tool_def, "name", "")) for tool_def in tools}
            if required_tool_name and required_tool_name in available_names:
                if self._is_llama_cpp_server():
                    api_kwargs["tool_choice"] = "required"
                else:
                    api_kwargs["tool_choice"] = {
                        "type": "function",
                        "function": {"name": required_tool_name},
                    }
            else:
                api_kwargs["tool_choice"] = "auto"
        if self.enable_response_format:
            api_kwargs["response_format"] = {"type": "json_object"}
        extra_body: Dict[str, Any] = {}
        if self.server_profile.get("name") != "auto":
            profile_body = self.server_profile.get("request_extra_body")
            if isinstance(profile_body, dict) and profile_body:
                extra_body = _deep_merge_dict(extra_body, profile_body)
        if self.enable_extra_body and self.extra_body:
            extra_body = _deep_merge_dict(extra_body, self.extra_body)
        mode_extra_body = self._mode_extra_body()
        if mode_extra_body:
            extra_body = _deep_merge_dict(extra_body, mode_extra_body)
        if self._reasoning_effort_metadata():
            # Profile-owned Qwen3.8 requests must use one canonical wire.  A
            # stale generic ``enable_thinking``/top-level effort from persisted
            # extra_body settings must not leak into the request projection.
            template_kwargs = extra_body.get("chat_template_kwargs")
            if isinstance(template_kwargs, dict):
                template_kwargs.pop("enable_thinking", None)
                effort = str(self._current_llm_mode or "").strip().lower()
                metadata = self._reasoning_effort_metadata() or {}
                if effort not in metadata.get("options", []):
                    effort = str(metadata.get("default") or "xhigh")
                template_kwargs["reasoning_effort"] = effort
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
        elif self._profile_disables_thinking():
            # Melody and other explicitly non-reasoning profiles must opt out
            # of template thinking even when no effort wire is declared.  A
            # user-provided true value cannot override the profile contract.
            template_kwargs = extra_body.get("chat_template_kwargs")
            if not isinstance(template_kwargs, dict):
                template_kwargs = {}
                extra_body["chat_template_kwargs"] = template_kwargs
            template_kwargs.pop("reasoning_effort", None)
            template_kwargs["enable_thinking"] = False
            extra_body.pop("reasoning_effort", None)
            extra_body.pop("enable_thinking", None)
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        self._cache_key = stable_cache_key(
            user_id=self._get_session_user_id(),
            session_id=getattr(self, "current_session_id", None),
            project_id=self.current_project_id,
            character=self.character_name,
            model=self.model_name,
            system_prompt=self.system_prompt,
            tool_schemas=api_kwargs.get("tools", []),
            provider="openai_compatible_local",
            branch_fingerprint=str(getattr(self, "current_edit_message_id", None) or "default-branch"),
            summary_version=int(getattr(self.history_manager, "summary_version", 0) or 0),
            server_instance=str(self.session_metadata.get("server_instance") or "default-instance"),
        )
        return api_kwargs

    def _chat_completion_tools(
        self,
        *,
        context_budget: Optional[ContextBudget] = None,
        required_tool_name: Optional[str] = None,
        user_input: Optional[str] = None,
    ) -> List[Any]:
        if (
            not getattr(self, "enable_tools", False)
            or _llama_cpp_profile_capability(
                getattr(self, "_llama_cpp_profile", None),
                "tools",
            )
            is False
        ):
            return []
        if len(self._tool_registry) <= 0:
            return []

        budget = context_budget or self._current_context_budget
        constrained = bool(budget) and self._is_constrained_context_budget(budget)
        allowed_names: set[str] = set()
        if constrained:
            allowed_names = self._constrained_native_tool_names(user_input)
            if allowed_names:
                # 狭コンテキストで名指しした deferred ツールは、対応する pack を
                # 先にロードしてから実効集合を作る。ロード手段自体を失わないよう
                # `load_tool_pack` も常に許可名へ含める。
                auto_load_packs_for_tool_names(
                    tool_pack_session_for_client(self),
                    allowed_names,
                )
                allowed_names = {*allowed_names, LOAD_TOOL_PACK_TOOL_NAME}

        tools = filter_tools_for_client(self, self._tool_registry.get_all())
        if required_tool_name:
            if not any(
                str(getattr(tool_def, "name", "")) == required_tool_name
                for tool_def in tools
            ):
                logger.warning(
                    "[OpenAICompatibleLocalClient] Required native tool is unavailable: %s",
                    required_tool_name,
                )

        if not constrained:
            return tools

        if not allowed_names:
            # No natural-language classifier selected a domain.  Keep the
            # model's normal choice available instead of silently hiding all
            # tools; deferred specialist packs remain represented by the
            # `load_tool_pack` meta-tool in ``tools``.
            return self._fit_native_tools_to_schema_budget(
                tools,
                budget,
                required_tool_name=required_tool_name,
            )
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
        return self._fit_native_tools_to_schema_budget(
            selected,
            budget,
            required_tool_name=required_tool_name,
        )

    def _constrained_native_tool_names(
        self,
        user_input: Optional[str],
    ) -> set[str]:
        text = str(user_input or "")
        if not text.strip():
            return set()

        names: set[str] = set()
        explicit_capabilities = set(
            sanitize_command_capabilities(
                getattr(self, "current_command_capabilities", ()) or ()
            )
        ) | command_capabilities_from_text(text)

        # Only trusted command capabilities and server-verified references
        # constrain a narrow schema.  Natural Docs/Files/Project/utility/media
        # wording must not hide the other tools from the model.
        if "web_search" in explicit_capabilities:
            names.add("web_search")
        if "project_progress_review" in explicit_capabilities:
            names.update(DIRECT_PROJECT_TOOL_HINT_NAMES)
        if "project_db_update" in explicit_capabilities:
            names.update(
                {
                    "organize_project_information_from_folder",
                    "patch_project_information_doc",
                    "attach_project_information_reference",
                }
            )
            names.update(
                {
                    "get_project_context",
                    "list_project_information",
                    "list_record_tables",
                }
            )
        if "task_update" in explicit_capabilities:
            names.update({"list_tasks", "create_task", "update_task"})
        if "wbs_sync" in explicit_capabilities:
            names.update({"sync_wbs_tasks", "get_upcoming_wbs_tasks", "list_tasks"})
        if "image_generation" in explicit_capabilities:
            names.add("media_assistant")
        if "workspace_file_operation" in explicit_capabilities:
            names.update(MANAGED_WORKSPACE_NATIVE_TOOL_NAMES)
        if looks_like_managed_workspace_request(text):
            # ``looks_like_managed_workspace_request`` is true only for the
            # trusted command capability or the server-set
            # ``TurnContext.verified_project_attachment`` flag.  Raw prompt
            # markers and ordinary workspace wording do not reach this path.
            names.update(MANAGED_WORKSPACE_NATIVE_TOOL_NAMES)
        return names

    def _fit_native_tools_to_schema_budget(
        self,
        tools: List[Any],
        context_budget: ContextBudget,
        *,
        required_tool_name: Optional[str] = None,
    ) -> List[Any]:
        if len(tools) <= 1:
            return tools
        ordered_tools = list(tools)
        required_name = str(required_tool_name or "").strip()
        if required_name:
            required_tools = [
                tool_def
                for tool_def in ordered_tools
                if str(getattr(tool_def, "name", "") or "") == required_name
            ]
            if required_tools:
                ordered_tools = [
                    *required_tools,
                    *[
                        tool_def
                        for tool_def in ordered_tools
                        if str(getattr(tool_def, "name", "") or "")
                        != required_name
                    ],
                ]
        limit = min(
            CONSTRAINED_NATIVE_TOOL_SCHEMA_CHAR_BUDGET,
            max(3500, int(context_budget.message_budget_chars * 0.9)),
        )
        selected: List[Any] = []
        selected_specs: list[Dict[str, Any]] = []
        for tool_def in ordered_tools:
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
        if len(selected) < len(ordered_tools):
            logger.warning(
                "[OpenAICompatibleLocalClient] Pruned native tool schemas for constrained local context: %s -> %s",
                len(ordered_tools),
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
        keys = {
            key: template_kwargs[key]
            for key in ("reasoning_effort", "enable_thinking")
            if key in template_kwargs
        }
        if not keys:
            return {}
        return {"chat_template_kwargs": keys}

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
            mode_extra_body = (
                self._mode_extra_body()
                if self._reasoning_effort_metadata()
                else self._mode_extra_body_from_kwargs(api_kwargs)
            )
            if mode_extra_body:
                if retry_kwargs["extra_body"] != mode_extra_body:
                    retry_kwargs["extra_body"] = mode_extra_body
                    removed.append("extra_body(non-mode)")
            else:
                retry_kwargs.pop("extra_body", None)
                removed.append("extra_body")
        return retry_kwargs, removed

    def _wait_for_local_server_health(self, *, timeout_seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            if self.health_check().get("ok"):
                return True
            time.sleep(0.5)
        return False

    def _served_local_model_ids(self) -> set[str]:
        served: set[str] = set()
        try:
            from src.service_manager import (
                _llama_cpp_model_ids_exact,
                _local_openai_model_ids,
            )

            served = {
                str(item).strip()
                for item in (_llama_cpp_model_ids_exact(self.base_url) or set())
                if str(item).strip()
            }
            if not served:
                served = {
                    str(item).strip()
                    for item in (_local_openai_model_ids(self.base_url) or set())
                    if str(item).strip()
                }
        except Exception:
            served = set()
        if served:
            return served
        try:
            return {
                str(item.get("id") or "").strip()
                for item in self.list_models()
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
        except Exception:
            return set()

    def _expected_local_model_is_served(self) -> bool:
        expected = str(self.model_name or "").strip()
        if not expected:
            return False
        return expected in self._served_local_model_ids()

    def _release_managed_local_generation_lease(self, *, all: bool = False) -> None:
        try:
            from src.service_manager import _release_llama_cpp_generation_lease_for_holder
        except Exception:
            return
        _release_llama_cpp_generation_lease_for_holder(self, all=all)

    def _ensure_managed_local_server_ready(
        self,
        *,
        acquire_generation_lease: bool = False,
        lease_tickets: list[Any] | None = None,
    ) -> bool:
        if not self._can_attempt_managed_llama_cpp_recovery():
            config_error = self._managed_llama_cpp_launch_configuration_error()
            if config_error:
                self._last_local_server_ensure_error = config_error
                self._mark_runtime_unavailable(config_error)
            elif self._is_manual_managed_llama_cpp_runtime():
                message = (
                    "手動接続先の llama-server に接続できません。"
                    " endpoint が起動しているか確認してください。"
                )
                self._last_local_server_ensure_error = message
                self._mark_runtime_unavailable(message)
            return False
        if not self.config:
            return False
        self._last_local_server_ensure_error = None
        ticket = None
        try:
            from src.service_manager import ensure_openai_compatible_local_server

            acquired: list[Any] = []
            try:
                ensure_openai_compatible_local_server(
                    self.config,
                    raise_on_launch_error=True,
                    force_restart=False,
                    model=self.model_name,
                    acquire_generation_lease=acquire_generation_lease,
                    lease_holder=self if acquire_generation_lease else None,
                    lease_tickets=acquired if acquire_generation_lease else None,
                )
            except TypeError as exc:
                # Keep lightweight test/dry-run adapters which implement the
                # pre-lease ensure signature working; the production service
                # manager accepts the request-scoped lease keywords above.
                if "acquire_generation_lease" not in str(exc):
                    raise
                ensure_openai_compatible_local_server(
                    self.config,
                    raise_on_launch_error=True,
                    force_restart=False,
                    model=self.model_name,
                )
            ticket = acquired[0] if acquired else None
            if self._expected_local_model_is_served():
                self._clear_runtime_unavailability()
                if ticket is not None and lease_tickets is not None:
                    lease_tickets.append(ticket)
                elif ticket is not None and lease_tickets is None:
                    ticket.release()
                return True
        except Exception as exc:
            if ticket is not None:
                ticket.release()
            self._last_local_server_ensure_error = str(exc)
            logger.warning(
                "[OpenAICompatibleLocalClient] Managed local server ensure failed: %s",
                exc,
            )
            self._mark_runtime_unavailable(str(exc))
            return False
        if ticket is not None:
            ticket.release()
        message = (
            "llama-server は応答していますが、"
            f"期待alias {str(self.model_name or '').strip()!r} が /v1/models にありません。"
        )
        self._last_local_server_ensure_error = message
        self._mark_runtime_unavailable(message)
        return False

    def _prepare_managed_local_server_for_request(self) -> Any:
        if self._can_attempt_managed_llama_cpp_recovery():
            tickets: list[Any] = []
            try:
                ready = self._ensure_managed_local_server_ready(
                    acquire_generation_lease=True,
                    lease_tickets=tickets,
                )
            except TypeError as exc:
                # Lightweight adapters may still expose the pre-lease helper
                # signature; production implementations use the ticket args.
                if "acquire_generation_lease" not in str(exc):
                    raise
                ready = self._ensure_managed_local_server_ready()
            if ready:
                return tickets[0] if tickets else None
            reason = str(self._last_local_server_ensure_error or "").strip()
            if not reason:
                reason = "llama-server の準備に失敗しました。"
                self._last_local_server_ensure_error = reason
            self._mark_runtime_unavailable(reason)
            raise ConnectionError(reason)
        config_error = self._managed_llama_cpp_launch_configuration_error()
        if config_error:
            self._last_local_server_ensure_error = config_error
            self._mark_runtime_unavailable(config_error)
            raise ConnectionError(config_error)
        return None

    def _create_completion_with_fallback(
        self,
        api_kwargs: Dict[str, Any],
        *,
        request_type: str = "chat",
        allow_connection_retry: bool = True,
    ) -> Any:
        self._sync_privacy_gateway()
        protected = self._privacy_gateway.protect_sync(
            api_kwargs,
            provider="openai_compatible_local",
            base_url=self.base_url,
            source_kind="model_request",
        )
        api_kwargs = protected.payload
        lease_ticket = self._prepare_managed_local_server_for_request()
        try:
            started_at = time.monotonic()
            try:
                self._capture_context_request(api_kwargs, reason="chat.completions")
                response = self.client.chat.completions.create(**api_kwargs)
                self._capture_and_persist_usage(
                    response,
                    request_type=request_type,
                    latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                )
                self._clear_runtime_unavailability()
                return response
            except Exception as exc:
                if _is_context_overflow_error(exc) and "tools" in api_kwargs:
                    retry_kwargs = dict(api_kwargs)
                    retry_kwargs.pop("tools", None)
                    retry_kwargs.pop("tool_choice", None)
                    logger.warning(
                        "[OpenAICompatibleLocalClient] Context overflow with native tools; retrying without tools: %s",
                        exc,
                    )
                    self._capture_context_request(retry_kwargs, reason="context_overflow_retry")
                    retry_started_at = time.monotonic()
                    response = self.client.chat.completions.create(**retry_kwargs)
                    self._capture_and_persist_usage(
                        response,
                        request_type="retry",
                        latency_ms=max(
                            0,
                            int((time.monotonic() - retry_started_at) * 1000),
                        ),
                    )
                    return response
                if _is_local_model_loading_error(exc) or _is_context_overflow_error(exc):
                    raise
                if allow_connection_retry and _is_connection_error(exc):
                    if self._can_attempt_managed_llama_cpp_recovery():
                        recovered = self._ensure_managed_local_server_ready()
                    elif self._managed_llama_cpp_launch_configuration_error():
                        config_error = self._managed_llama_cpp_launch_configuration_error()
                        self._last_local_server_ensure_error = str(config_error or "")
                        self._mark_runtime_unavailable(self._last_local_server_ensure_error)
                        recovered = False
                    elif self._is_manual_managed_llama_cpp_runtime():
                        message = (
                            "手動接続先の llama-server に接続できません。"
                            " endpoint が起動しているか確認してください。"
                        )
                        self._last_local_server_ensure_error = message
                        self._mark_runtime_unavailable(message)
                        recovered = False
                    else:
                        recovered = self._wait_for_local_server_health(timeout_seconds=5.0)
                    if recovered:
                        logger.warning(
                            "[OpenAICompatibleLocalClient] Connection error; retrying after local server recovery: %s",
                            exc,
                        )
                        return self._create_completion_with_fallback(
                            api_kwargs,
                            request_type=request_type,
                            allow_connection_retry=False,
                        )
                if _is_connection_error(exc):
                    if not self.is_runtime_known_unavailable():
                        reason = str(self._last_local_server_ensure_error or exc)
                        self._mark_runtime_unavailable(reason)
                    raise
                retry_kwargs, removed = self._compatibility_retry_kwargs(api_kwargs)
                if not removed:
                    raise
                logger.warning(
                    "[OpenAICompatibleLocalClient] Compatibility retry without %s: %s",
                    ", ".join(removed),
                    exc,
                )
                self._capture_context_request(retry_kwargs, reason="compatibility_retry")
                retry_started_at = time.monotonic()
                response = self.client.chat.completions.create(**retry_kwargs)
                self._capture_and_persist_usage(
                    response,
                    request_type="retry",
                    latency_ms=max(0, int((time.monotonic() - retry_started_at) * 1000)),
                )
                return response
        finally:
            if lease_ticket is not None:
                lease_ticket.release()

    def _message_content(self, message: Any) -> str:
        content = _message_field(message, "content")
        if not isinstance(content, str):
            return ""
        return _strip_leading_think_markup(content)

    def _record_model_transcript(
        self,
        messages: List[Dict[str, Any]],
        response_text: str,
    ) -> None:
        source_messages = self._last_tool_loop_messages or messages
        self._last_model_transcript = [
            dict(message)
            for message in source_messages
            if message.get("role") in {"user", "assistant", "tool"}
        ]
        if response_text:
            self._last_model_transcript.append(
                {"role": "assistant", "content": response_text}
            )
        if hasattr(self.history_manager, "set_model_messages"):
            self.history_manager.set_model_messages(self._last_model_transcript)

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
        # Managed Qwen3.8 has no disable-thinking switch.  A reasoning-only
        # response may still be retried, but the same profile effort must be
        # preserved instead of adding enable_thinking=false.
        if self._reasoning_effort_metadata():
            return _message_has_reasoning_output(message)
        if self._chat_template_thinking_value(api_kwargs) is False:
            return False
        if _message_has_reasoning_output(message):
            return True
        return False

    def _create_completion_retrying_empty_reasoning(
        self,
        api_kwargs: Dict[str, Any],
        *,
        reason: str,
        request_type: str = "chat",
    ) -> Any:
        response = self._create_completion_with_fallback(
            api_kwargs,
            request_type=request_type,
        )
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
        if self._reasoning_effort_metadata():
            retry_kwargs = dict(api_kwargs)
            retry_extra = retry_kwargs.get("extra_body")
            retry_extra = retry_extra if isinstance(retry_extra, dict) else {}
            retry_extra = _deep_merge_dict(retry_extra, self._mode_extra_body())
            retry_template = retry_extra.get("chat_template_kwargs")
            if isinstance(retry_template, dict):
                retry_template.pop("enable_thinking", None)
            retry_extra.pop("reasoning_effort", None)
            retry_extra.pop("enable_thinking", None)
            retry_kwargs["extra_body"] = retry_extra
        else:
            retry_kwargs = self._set_chat_template_thinking(api_kwargs, False)
        api_kwargs.clear()
        api_kwargs.update(retry_kwargs)
        return self._create_completion_with_fallback(
            api_kwargs,
            request_type="retry",
        )

    def _empty_response_fallback(self) -> str:
        return LOCAL_MODEL_EMPTY_RESPONSE

    def _tool_loop_completion(self, api_kwargs: Dict[str, Any]) -> Any:
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="tool follow-up",
            request_type="tool",
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
            request_type="retry",
        )
        self._capture_generation_metrics(response)
        message = response.choices[0].message
        content = self._message_content(message)
        if content:
            return self._privacy_gateway.restore(
                guard_tool_execution_claims(content, tool_calls)
            )
        if "extra_body" in api_kwargs:
            retry_kwargs = dict(api_kwargs)
            if self._reasoning_effort_metadata():
                # Keep the selected managed-profile effort on the final
                # fallback too.  Dropping extra_body here would silently
                # change a low/medium/xhigh request into template default.
                retry_kwargs["extra_body"] = self._mode_extra_body()
            else:
                retry_kwargs.pop("extra_body", None)
            logger.warning(
                "[OpenAICompatibleLocalClient] Minimal empty-response retry returned empty; retrying with %s extra_body (%s)",
                "managed effort" if self._reasoning_effort_metadata() else "no",
                reason,
            )
            response = self._create_completion_with_fallback(
                retry_kwargs,
                request_type="retry",
            )
            message = response.choices[0].message
            content = self._message_content(message)
            if content:
                return self._privacy_gateway.restore(
                    guard_tool_execution_claims(content, tool_calls)
                )
        return ""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        request_type: str = "chat",
        tools_enabled: bool = True,
        context_budget: Optional[ContextBudget] = None,
        fallback_user_input: Optional[str] = None,
        required_tool_name: Optional[str] = None,
        native_tool_user_input: Optional[str] = None,
        event_callback: Optional[SyncStreamEmitter] = None,
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
            request_type=request_type,
        )
        self._capture_generation_metrics(response)
        choice = response.choices[0]
        if getattr(choice.message, "tool_calls", None):
            result = self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
                required_tool_name=required_tool_name,
                event_callback=event_callback,
            )
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
        content = self._message_content(choice.message)
        if fallback_user_input and project_progress_review_active(fallback_user_input):
            result = self._handle_tool_calls(
                messages,
                choice.message,
                api_kwargs,
                context_budget or self._context_budget(max_tokens),
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_user_input=fallback_user_input,
                required_tool_name=required_tool_name,
                event_callback=event_callback,
            )
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
        # ツール往復が無い応答でも reasoning が返っていれば thinking として配信する。
        emit_thinking(
            event_callback,
            thinking_text_from_message(choice.message),
            round_index=0,
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
            result = guard_tool_execution_claims(content, [])
            self._record_model_transcript(messages, result)
            return self._privacy_gateway.restore(result)
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
                return self._privacy_gateway.restore(retry_content)
        return self._empty_response_fallback()

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        self._sync_privacy_gateway()
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
        stream_kwargs = self._privacy_gateway.protect_sync(
            stream_kwargs,
            provider="openai_compatible_local",
            base_url=self.base_url,
            source_kind="model_request",
        ).payload
        lease_ticket = self._prepare_managed_local_server_for_request()
        try:
            self._capture_context_request(stream_kwargs, reason="stream")
            stream = self.client.chat.completions.create(**stream_kwargs)
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield self._privacy_gateway.restore_aliases(
                        chunk.choices[0].delta.content
                    )
                previous_usage = dict(getattr(self, "_last_usage", {}) or {})
                captured_usage = self._capture_generation_metrics(chunk)
                # Some servers attach usage only to an intermediate/final chunk;
                # do not erase the last confirmed metrics when a content-only
                # chunk follows it.
                if not captured_usage and previous_usage:
                    self._last_usage = previous_usage
        finally:
            if lease_ticket is not None:
                lease_ticket.release()

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
        required_tool_name: Optional[str] = None,
        max_rounds: int = 5,
        event_callback: Optional[SyncStreamEmitter] = None,
    ) -> str:
        effective_max_rounds = max(
            max_rounds,
            agentic_max_rounds(self, fallback_user_input),
        )

        # llama.cpp requires the initial grounding tool to be selected with a
        # native ``tool_choice=required`` request.  Its initial schema is
        # intentionally narrowed to that single tool, but reusing those
        # kwargs for the follow-up would permanently hide mutation tools.  The
        # assistant message already contains the required read call, so
        # rebuild a normal effective schema for the loop's subsequent calls.
        loop_api_kwargs = api_kwargs
        if required_tool_name and self._is_llama_cpp_server():
            loop_api_kwargs = self._build_api_kwargs(
                messages,
                temperature,
                max_tokens,
                context_budget=context_budget,
                tools_enabled=True,
                required_tool_name=None,
                native_tool_user_input=fallback_user_input,
            )
        result = run_openai_tool_call_loop(
            initial_messages=messages,
            assistant_message=assistant_message,
            api_kwargs=loop_api_kwargs,
            registry=filtered_registry_for_client(self, self._tool_registry),
            create_completion=self._tool_loop_completion,
            log_prefix="OpenAICompatibleLocalClient",
            max_rounds=effective_max_rounds,
            return_result=True,
            max_tool_result_chars=context_budget.tool_result_chars,
            message_content=self._message_content,
            config=self.config,
            user_input=fallback_user_input,
            require_project_context_read=(
                self._requires_project_context_read(fallback_user_input)
                and not self._project_context_read_already_satisfied()
            ),
            skip_final_response_check_on_empty=True,
            event_callback=event_callback,
            restore_tool_arguments=self._privacy_gateway.restore_tool_arguments,
        )
        self._last_tool_calls.extend(result.tool_calls)
        self._last_tool_loop_messages = [
            dict(message) for message in getattr(result, "messages", [])
        ]
        execution_records = list(getattr(result, "audit_tool_calls", None) or [])
        self._last_audit_tool_calls.extend(execution_records)
        self._last_tool_loop_completion_confirmed = tool_loop_completion_confirmed(
            execution_records,
            result.final_output,
            stopped_reason=getattr(result, "stopped_reason", None),
        )
        if result.final_output:
            return self._privacy_gateway.restore(
                guard_tool_execution_claims(result.final_output, result.tool_calls)
            )
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
                return self._privacy_gateway.restore(retry_content)
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
                    "You are a careful AoiTalk completion verifier. "
                    "Return exactly one JSON review object and do not call tools."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        return self.chat(
            review_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            request_type="review",
            tools_enabled=False,
            context_budget=context_budget,
            fallback_user_input=user_input,
            native_tool_user_input=user_input,
        )

    def _run_agentic_continuation_once(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: Optional[int],
        context_budget: ContextBudget,
        user_input: str,
        event_callback: Optional[SyncStreamEmitter] = None,
        system_content: str = "",
    ) -> str:
        continuation_messages = [
            {
                "role": "system",
                "content": (
                    system_content
                    or self._current_turn_system_content
                    or (
                        self._system_prompt_for_budget(context_budget)
                        + "\n\n"
                        + _current_date_context()
                    )
                ),
            },
            {"role": "user", "content": prompt},
        ]
        required_tool_name = self._required_tool_name(user_input)
        return self.chat(
            continuation_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            request_type="continuation",
            tools_enabled=True,
            context_budget=context_budget,
            fallback_user_input=user_input,
            required_tool_name=required_tool_name,
            native_tool_user_input=user_input,
            event_callback=event_callback,
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
        self._last_generation_metrics = None
        self._last_context_snapshots = []
        self._last_tool_calls = []
        self._last_agentic_events = []
        self._last_model_transcript = []
        self._last_usage = {}
        self._last_tool_loop_messages = []
        self._last_tool_loop_completion_confirmed = False
        self._last_audit_tool_calls = []
        self._current_turn_system_content = ""
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
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            self._sync_privacy_gateway()
            messages, tool_hint_context = self._build_model_messages_for_budget(
                user_input,
                project_context,
                context_budget,
            )
            if image_data and messages:
                messages[-1]["content"] = openai_content_parts(
                    str(messages[-1].get("content") or ""),
                    image_data,
                )
            self._current_turn_system_content = str(
                messages[0].get("content") or ""
            )
            required_tool_name = self._required_tool_name(user_input)
            if stream:
                return self._stream_response(messages, temperature, max_tokens, user_input)
            turn_event_emitter = make_sync_stream_emitter(stream_callback)
            response_text = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools_enabled=True,
                context_budget=context_budget,
                fallback_user_input=user_input,
                required_tool_name=required_tool_name,
                native_tool_user_input=user_input,
                event_callback=turn_event_emitter,
            )

            def _agentic_event_callback(event_type: str, data: Dict[str, Any]) -> None:
                event_data = dict(data or {})
                event_data["event_type"] = event_type
                self._last_agentic_events.append(event_data)
                if stream_callback:
                    self._run_async_sync(stream_callback(event_type, data))

            if self._should_run_agentic_completion_review():
                response_text = run_agentic_completion_loop_sync(
                    client=self,
                    run_once=lambda prompt: self._run_agentic_continuation_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        system_content=self._current_turn_system_content,
                    ),
                    run_review_once=lambda prompt: self._run_agentic_review_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                    ),
                    run_continuation_once=lambda prompt: self._run_agentic_continuation_once(
                        prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        context_budget=context_budget,
                        user_input=user_input,
                        event_callback=turn_event_emitter,
                        system_content=self._current_turn_system_content,
                    ),
                    context=render_messages_for_review(messages),
                    user_input=user_input,
                    initial_response=response_text,
                    event_callback=_agentic_event_callback,
                    tool_evidence_provider=self._format_last_tool_calls_evidence,
                    completion_confirmed_provider=lambda: self._last_tool_loop_completion_confirmed,
                    audit_tool_calls_provider=lambda: list(self._last_audit_tool_calls),
                )
            self._record_model_transcript(messages, response_text)
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response_text)
            return response_text
        except GenerationInterrupted:
            raise
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
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="title generation",
            request_type="title",
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

    def _generate_plain_text(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        request_type: str = "plain",
    ) -> str:
        """Generate text without normal chat context, tools, or history."""
        self._last_generation_metrics = None
        context_budget = self._context_budget(1024)
        messages = [
            {
                "role": "system",
                "content": system_prompt
                or "You are a concise assistant. Do not call tools or modify files.",
            },
            {"role": "user", "content": prompt},
        ]
        api_kwargs = self._build_api_kwargs(
            messages,
            temperature=0.2,
            max_tokens=1024,
            context_budget=context_budget,
            tools_enabled=False,
        )
        api_kwargs.pop("response_format", None)
        response = self._create_completion_retrying_empty_reasoning(
            api_kwargs,
            reason="ephemeral text generation",
            request_type=request_type,
        )
        self._capture_generation_metrics(response)
        return self._message_content(response.choices[0].message)

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate text without mutating normal conversation history."""
        return await asyncio.to_thread(
            self._generate_plain_text,
            prompt,
            system_prompt=system_prompt,
            request_type="plain",
        )

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """Generate memory extraction text without mutating chat history."""

        return await asyncio.to_thread(
            self._generate_plain_text,
            prompt,
            system_prompt=system_prompt,
            request_type="memory_extraction",
        )

    def _stream_response(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        user_input: str,
    ) -> Generator[str, None, None]:
        full_response = ""
        # Keep a request-local ledger.  ``_last_usage`` is also used by
        # non-streaming calls, so reset it before starting and never persist a
        # value left over from an earlier request when stream creation fails
        # or yields no chunks.
        self._last_usage = {}
        stream_usage: Dict[str, Any] = {}
        try:
            for content in self.stream_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if self._last_usage:
                    stream_usage = dict(self._last_usage)
                full_response += content
                yield content
        except GenerationInterrupted:
            raise
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
            self._record_model_transcript(messages, full_response)
            # The final stream chunk can carry usage without content; retain
            # that observation while avoiding stale usage on an empty/failed
            # stream.  Persist exactly once for the whole stream.
            if self._last_usage:
                stream_usage = dict(self._last_usage)
            self._last_usage = dict(stream_usage)
            if stream_usage:
                persist_usage_sync(
                    self,
                    provider="openai_compatible_local",
                    model=self.model_name,
                    usage=stream_usage,
                    request_type="chat",
                    is_streaming=True,
                )
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", full_response)

    def _get_error_response(self, exc: Exception) -> str:
        if _is_local_model_loading_error(exc):
            return LOCAL_MODEL_LOADING_RESPONSE
        if _is_context_overflow_error(exc):
            return self._get_context_overflow_fallback_response()
        if _is_connection_error(exc):
            config_error = self._managed_llama_cpp_launch_configuration_error()
            if config_error:
                try:
                    from src.service_manager import llama_cpp_runtime_requirement

                    requirement = llama_cpp_runtime_requirement(
                        self.config,
                        model=self.model_name,
                    )
                except Exception:
                    requirement = None
                requirement_hint = (
                    f" {requirement}"
                    if requirement
                    else ""
                )
                return (
                    "ローカルLLMサーバー（llama-server）を起動できませんでした。"
                    "logs/models/llama_cpp.log を確認し、"
                    "選択したprofileのruntime要件を満たすllama.cppを用意してから再試行してください。"
                    f"{requirement_hint}"
                    f"\n\n詳細:\n{config_error}"
                )
            if self._can_attempt_managed_llama_cpp_recovery() or self._uses_llama_cpp_tool_choice_transport():
                detail = str(self._last_local_server_ensure_error or "").strip()
                if detail:
                    return f"{LOCAL_MODEL_SERVER_START_FAILED_RESPONSE}\n\n詳細: {detail}"
                return LOCAL_MODEL_SERVER_START_FAILED_RESPONSE
            return "ローカルLLMサーバーへ接続できません。サーバーが起動しているか、base_url 設定を確認してください。"
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
        context_bundle = self._context_bundle_for_turn()
        if context_bundle:
            evidence_blocks.extend(
                [
                    context_bundle.project_context_block,
                    context_bundle.project_information_block,
                    context_bundle.project_pack_block,
                ]
            )
        evidence = "\n\n".join(block.strip() for block in evidence_blocks if block and block.strip())
        if evidence:
            parts.append(clip_text(evidence, budget.context_bundle_chars))
        else:
            parts.append(
                "取得済みの案件情報Docs・ツール実行結果・会話根拠はありませんでした。"
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
        self._release_managed_local_generation_lease(all=True)
        logger.info("[OpenAICompatibleLocalClient] cleanup complete")


def create_openai_compatible_local_client(config: Config) -> OpenAICompatibleLocalClient:
    local_config = _config_get(config, "openai_compatible_local", {}) or {}
    response_model = (
        _config_get(config, "llm_model")
        if _config_get(config, "response_model_selection_active")
        else None
    )
    model = (
        _config_get(config, "runtime.target_model")
        or response_model
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_MODEL")
        or _config_get(config, "llm_model")
        or _config_get(config, "openai_compatible_local.model")
        or DEFAULT_LOCAL_MODEL
    )
    base_url = _config_get(
        config,
        "runtime.target_base_url",
    ) or openai_compatible_local_base_url(config, model=model)
    api_key = (
        _config_get(config, "runtime.target_api_key")
        or os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
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

