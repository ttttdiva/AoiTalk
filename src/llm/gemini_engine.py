"""
Gemini LLM engine implementation with Function Calling support
"""

import os
import asyncio
import copy
import inspect
import threading
import concurrent.futures
import uuid
import json
import logging
import io
import mimetypes
from collections.abc import Mapping, Sequence
from typing import Optional, List, Dict, Any, Union, Generator
import google.generativeai as genai
from google.api_core.exceptions import DeadlineExceeded
from google.generativeai.types import (
    HarmCategory,
    HarmBlockThreshold,
    FunctionDeclaration,
    Tool,
)

from ..config import Config
from ..tools.registry import ToolRegistry, get_registry
from ..tools.adapters import GeminiAdapter
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    get_runtime_project_context,
    project_context_enabled_for_client,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from .manager_parts.context_building import strip_project_context_bundle
from ..services.turn_context import (
    AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
    get_turn_context,
)
from ..services.story_chat_context import (
    build_story_chat_context,
    run_story_chat_context_sync,
    is_story_workflow_tool_allowed,
)
from .prompts import build_unified_instructions
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import ensure_load_tool_pack_tool
from .tool_exposure import (
    filtered_registry_for_client,
    get_strict_tool_allowlist,
    is_review_generation,
)
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    GenerationProfile,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .generation_cancellation import GenerationInterrupted, PlanningInteractionTerminated
from .agentic_completion import (
    render_messages_for_review,
    run_agentic_completion_loop_sync,
)
from .agent_runtime import (
    OpenAIToolCallRecord,
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
)
from .tool_policy import reset_current_user_input, set_current_user_input
from .turn_stream_events import (
    bind_stream_callback_loop,
    emit_assistant_text,
    emit_thinking,
    emit_tool_end,
    emit_tool_start,
    make_sync_stream_emitter,
)
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall, UnifiedToolResult
from .multimodal import data_url_to_bytes, normalize_image_payloads
from .conversation_context import persist_usage_sync
from .context_snapshot import (
    capture_context_manifest_before_context_clear,
    component,
    context_manifest_metadata,
    context_bundle_components,
    message_components,
    reconcile_snapshot,
    sanitized_snapshot_series,
    snapshot,
    without_text,
)
from ..services.user_settings_service import get_user_custom_instructions_sync
from ..services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from ..utils.logging_config import FILE_ONLY_LOG_EXTRA
from ..utils.startup_console import safe_startup_reason


logger = logging.getLogger(__name__)


def _gemini_part_is_thought(part: Any) -> bool:
    """thought フラグ付き part かどうかを SDK 差異に耐える形で判定する。

    ``include_thoughts`` 非対応の SDK では ``thought`` 属性自体が無いため、
    その場合は常に False を返し従来動作のままとする。
    """
    try:
        return bool(getattr(part, "thought", False))
    except Exception:
        return False


_GEMINI_STORY_CONTEXT_UNSET = object()


def _plain_json_value(value: Any, *, path: str = "arguments") -> Any:
    """Convert Gemini proto containers into ordinary JSON values.

    ``google.generativeai`` exposes function-call arguments through
    ``MapComposite``/``RepeatedComposite`` rather than builtin ``dict``/``list``
    instances.  Those values must never be allowed to cross the tool boundary
    (or leak into durable JSON) as provider objects.  Unknown provider values
    fail closed instead of being stringified into executable arguments.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _plain_json_value(item, path=f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _plain_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        return [
            _plain_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]

    # Some SDK releases expose nested messages with a ``to_dict`` helper but
    # do not register them as Mapping.  Re-enter the strict conversion after
    # obtaining that plain representation.
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception as exc:  # pragma: no cover - provider-specific
            raise ValueError(f"unsupported Gemini value at {path}") from exc
        if converted is value:
            raise ValueError(f"unsupported Gemini value at {path}")
        return _plain_json_value(converted, path=path)

    raise ValueError(
        f"unsupported Gemini provider value at {path}: {type(value).__name__}"
    )


def _plain_json_object(value: Any, *, path: str = "arguments") -> dict[str, Any]:
    converted = _plain_json_value(value, path=path)
    if not isinstance(converted, dict):
        raise ValueError(f"Gemini {path} must be an object")
    return converted


class _GeminiAgenticReviewDeadline(Exception):
    """Internal sentinel separating optional review timeout from continuation."""


def _declaration_names(tools: Any) -> tuple[str, ...]:
    names: list[str] = []
    for tool in list(tools or []):
        for declaration in list(getattr(tool, "function_declarations", []) or []):
            names.append(str(getattr(declaration, "name", "")))
    return tuple(names)


def _fingerprint_declaration_value(value: Any) -> Any:
    """Normalize Gemini SDK values for schema-sensitive model reuse checks."""
    if isinstance(value, dict):
        return tuple(
            (str(key), _fingerprint_declaration_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_fingerprint_declaration_value(item) for item in value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _fingerprint_declaration_value(to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _fingerprint_declaration_value(vars(value))
        except Exception:
            pass
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _declaration_fingerprints(tools: Any) -> tuple[Any, ...]:
    fingerprints: list[Any] = []
    for tool in list(tools or []):
        for declaration in list(getattr(tool, "function_declarations", []) or []):
            fingerprints.append(
                (
                    str(getattr(declaration, "name", "")),
                    str(getattr(declaration, "description", "")),
                    _fingerprint_declaration_value(
                        getattr(declaration, "parameters", None)
                    ),
                )
            )
    return tuple(fingerprints)


def _same_function_declarations(left: Any, right: Any) -> bool:
    """宣言の中身が同じなら既定モデルを再利用してよいと判断する。"""
    if left is right:
        return True
    return _declaration_fingerprints(left) == _declaration_fingerprints(right)


def _gemini_generation_config(
    config_kwargs: Dict[str, Any],
) -> tuple[Any, bool]:
    """GenerationConfig を組み立てる。include_thoughts 非対応なら段階的に外す。

    Returns:
        (generation_config, include_thoughts が有効か)
    """
    thinking_config = config_kwargs.get("thinking_config")
    includes_thoughts = bool(
        isinstance(thinking_config, dict) and thinking_config.get("include_thoughts")
    )
    try:
        return genai.types.GenerationConfig(**config_kwargs), includes_thoughts
    except Exception as exc:
        if not includes_thoughts:
            raise
        logger.info(
            "Gemini include_thoughts is unsupported; omitting it (%s)",
            safe_startup_reason(exc),
            extra=FILE_ONLY_LOG_EXTRA,
        )
        return _gemini_generation_config_without_thoughts(config_kwargs), False


def _gemini_generation_config_without_thoughts(config_kwargs: Dict[str, Any]) -> Any:
    """include_thoughts を外した GenerationConfig を返す（駄目なら thinking_config ごと外す）。"""
    fallback_kwargs = dict(config_kwargs)
    thinking_config = fallback_kwargs.get("thinking_config")
    if isinstance(thinking_config, dict):
        reduced = {
            key: value
            for key, value in thinking_config.items()
            if key != "include_thoughts"
        }
        if reduced:
            fallback_kwargs["thinking_config"] = reduced
        else:
            fallback_kwargs.pop("thinking_config", None)
    try:
        return genai.types.GenerationConfig(**fallback_kwargs)
    except Exception as exc:
        logger.info(
            "Gemini thinking_config was disabled after configuration fallback (%s)",
            safe_startup_reason(exc),
            extra=FILE_ONLY_LOG_EXTRA,
        )
        fallback_kwargs.pop("thinking_config", None)
        return genai.types.GenerationConfig(**fallback_kwargs)


class GeminiLLMClient:
    """Gemini LLM client for character-based responses"""

    # ``generate_response`` loads the durable ConversationSession when the
    # requested current_session_id changes, so ingress must not prefill a
    # second user-scoped history copy before the provider request.
    manages_conversation_session_history = True

    DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
    MIN_REQUEST_TIMEOUT_SECONDS = 1
    MAX_REQUEST_TIMEOUT_SECONDS = 300

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3-flash-preview",
        config: Optional[Config] = None,
    ):
        """Initialize Gemini LLM client with Function Calling support

        Args:
            api_key: Google AI API key
            model: Gemini model to use
            config: Application configuration
        """
        from ..memory.history import HistoryManager

        self.config = config
        self._lightweight_ephemeral_client = bool(
            config
            and config.get(
                "runtime.lightweight_ephemeral_client",
                False,
            )
        )
        target_tools = (
            config.get("runtime.target_enable_tools", None)
            if config
            else None
        )
        self._target_tools_enabled = target_tools is not False
        if self._lightweight_ephemeral_client:
            self._target_tools_enabled = False
        self._privacy_gateway = OutboundPrivacyGateway(config)
        self.character_name = (
            "Assistant"
            if self._lightweight_ephemeral_client
            else config.default_character if config else "Assistant"
        )
        self.conversation_history = []
        self.history_manager = HistoryManager()
        self.model_name = model
        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = (
            None  # For session-specific message storage and history loading
        )
        self.current_assistant_message_id: Optional[str] = None
        self.current_project_id: Optional[str] = (
            None  # For project-specific session creation
        )
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._last_context_snapshots: List[Dict[str, Any]] = []
        self._context_request_index = 0
        self._loaded_session_id: Optional[str] = (
            None  # Track which session's history is already loaded
        )
        self._history_lock = (
            threading.Lock()
        )  # Protect conversation_history from concurrent access

        # Initialize memory manager
        self.memory_manager = None
        self._memory_enabled = (
            config.get("memory", {}).get("enabled", True) if config else True
        )
        if config and (
            bool(config.get("runtime.ephemeral_session_client", False))
            or config.get("memory.enabled") is False
        ):
            self._memory_enabled = False
        self._cleanup_done = False
        self._memory_loop: Optional[asyncio.AbstractEventLoop] = None
        self._memory_thread: Optional[threading.Thread] = None
        self._memory_warmup_future = None
        if self._memory_enabled:
            memory_config = MemoryConfig()
            if config:
                memory_settings = config.get("memory", {})
                memory_config.llm_provider = config.get(
                    "llm_provider", memory_config.llm_provider
                )
                memory_config.llm_model = config.get(
                    "llm_model", memory_config.llm_model
                )
                memory_config.enable_search = memory_settings.get("enable_search", True)
            self.memory_manager = ConversationMemoryManager(memory_config)

            # Start persistent memory event loop thread
            self._memory_loop = asyncio.new_event_loop()
            self._memory_thread = threading.Thread(
                target=self._run_memory_loop, daemon=True, name="gemini-memory-loop"
            )
            self._memory_thread.start()

            memory_settings = config.get("memory", {}) if config else {}
            if memory_settings.get("enable_search", True):
                # Pre-warm cross-session memory only when semantic memory search is enabled.
                self._memory_warmup_future = asyncio.run_coroutine_threadsafe(
                    self._warmup_cross_session_memory(), self._memory_loop
                )

        # Configure Gemini
        genai.configure(api_key=api_key)

        # Initialize system prompt based on character
        if self._lightweight_ephemeral_client:
            self.system_prompt = (
                "Follow the supplied instruction exactly and return only "
                "the requested content. Do not use tools or character context."
            )
        else:
            self.system_prompt = self._build_system_prompt()

        if self._lightweight_ephemeral_client or not self._target_tools_enabled:
            self._tool_registry = ToolRegistry()
        elif config:
            self._tool_registry = build_runtime_tool_registry_for_client(
                build_runtime_tool_registry,
                config,
                client=self,
            )
            # `load_tool_pack` はクライアント固有の ToolPackSession を閉じ込めるため、
            # このクライアント専用レジストリにだけ登録する。
            ensure_load_tool_pack_tool(self._tool_registry, self)
        else:
            # config なし経路はプロセス共有レジストリを使うので、セッション状態を
            # 持つメタツールを混ぜて他クライアントと混線させない。
            self._tool_registry = get_registry()

        # Initialize available tools from unified registry
        self.tools = (
            self._setup_tools()
            if self._target_tools_enabled
            and not self._lightweight_ephemeral_client
            else []
        )

        # Initialize model with safety settings and tools
        self._safety_settings = {
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        }

        self.model = genai.GenerativeModel(
            model_name=model, safety_settings=self._safety_settings, tools=self.tools
        )

        logger.info(
            "Gemini model initialized: %s",
            model,
        )

        # Initialize Spotify
        if self.config and not self._lightweight_ephemeral_client:
            from ..tools import init_spotify_manager

            spotify_success = init_spotify_manager()
            if spotify_success:
                logger.info(
                    "Spotify integration initialized for Gemini",
                )
            else:
                logger.info(
                    "Spotify integration skipped for Gemini (configuration incomplete)",
                )

        logger.info(
            "Gemini client initialized: character=%s model=%s tools=%s",
            self.character_name,
            model,
            len(self._tool_registry),
        )

    def _gemini_request_timeout_seconds(self) -> int:
        configured = (
            self.config.get("gemini.request_timeout_seconds")
            if getattr(self, "config", None)
            else None
        )
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = self.DEFAULT_REQUEST_TIMEOUT_SECONDS
        if value < self.MIN_REQUEST_TIMEOUT_SECONDS:
            return self.DEFAULT_REQUEST_TIMEOUT_SECONDS
        return min(value, self.MAX_REQUEST_TIMEOUT_SECONDS)

    def _send_message_with_deadline(
        self,
        chat: Any,
        message: Any,
        *,
        generation_config: Any,
        tools: Any = None,
        tool_config: Any = None,
        privacy_payload: Any = None,
    ) -> Any:
        """Send one Gemini chat turn with the configured transport deadline."""

        kwargs: dict[str, Any] = {
            "generation_config": generation_config,
            "request_options": {"timeout": self._gemini_request_timeout_seconds()},
        }
        # The deprecated SDK accepts ``tools``/``tool_config`` on
        # ChatSession.send_message.  Lightweight compatibility fakes in older
        # callers may expose only the historical three parameters, so inspect
        # an explicit signature and omit these optional keywords for them.
        try:
            parameters = inspect.signature(chat.send_message).parameters
            accepts_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            parameters = {}
            accepts_var_kwargs = True
        if tools is not None and (
            accepts_var_kwargs or "tools" in parameters
        ):
            kwargs["tools"] = tools
        if tool_config is not None and (
            accepts_var_kwargs or "tool_config" in parameters
        ):
            kwargs["tool_config"] = tool_config
        # Keep the provider call inside the privacy transaction.  The
        # historical implementation protected the prompt in a separate step
        # and then sent it later, which left a TOCTOU gap for review/approval
        # and allowed a caller to accidentally send the pre-redaction value.
        gateway = getattr(self, "_privacy_gateway", None)
        if not isinstance(gateway, OutboundPrivacyGateway):
            gateway = OutboundPrivacyGateway(getattr(self, "config", None))
            self._privacy_gateway = gateway

        request_payload: dict[str, Any] = {
            "message": message,
            # ``ChatSession.send_message`` implicitly resends its history.
            # Include that provider-managed state in the same transaction so
            # a review/redaction decision covers the complete wire payload,
            # not only the latest user part.
            "history": list(getattr(chat, "history", None) or []),
            **kwargs,
        }
        if privacy_payload is not None:
            request_payload["privacy_payload"] = privacy_payload
        base_url = str(
            getattr(getattr(self, "config", None), "get", lambda *_: "")(
                "gemini.base_url", ""
            )
            or "https://generativelanguage.googleapis.com"
        )
        descriptor = EgressDescriptor(
            action="model.generate",
            transport="gemini.chat.send_message",
            destination=base_url,
            provider="gemini",
            model=str(getattr(self, "model_name", "") or ""),
        )

        def send_message(outbound: dict[str, Any]) -> Any:
            if not isinstance(outbound, dict) or "message" not in outbound:
                raise PrivacyError("Gemini outbound message is missing")
            outbound_kwargs = dict(outbound)
            outbound_message = outbound_kwargs.pop("message")
            if outbound_message is None:
                raise PrivacyError("Gemini outbound message is empty")
            outbound_history = outbound_kwargs.pop("history", None)
            if outbound_history is None:
                raise PrivacyError("Gemini outbound history is missing")
            # ``privacy_payload`` is a gateway-only evidence field (for
            # example raw attachment bytes).  It must never be forwarded to
            # the provider SDK; the actual media remains in ``message``.
            outbound_kwargs.pop("privacy_payload", None)
            if outbound_history is not None:
                try:
                    chat.history = outbound_history
                except Exception:
                    # Some SDK chat fakes expose read-only history.  The
                    # latest message is still passed through the gateway;
                    # retaining the existing state is the safest fallback.
                    pass
            return chat.send_message(outbound_message, **outbound_kwargs)

        return gateway.execute_sync(
            request_payload,
            provider="gemini",
            descriptor=descriptor,
            sender=send_message,
            base_url=base_url,
            source_kind="model_request",
            model=str(getattr(self, "model_name", "") or ""),
        )

    def set_tool_registry(self, registry: ToolRegistry) -> None:
        """専門委譲用に許可済みtoolだけでGemini宣言を再構築する。"""

        self._tool_registry = registry
        self.tools = self._setup_tools()
        self.model = genai.GenerativeModel(
            model_name=self.model_name,
            safety_settings=self._safety_settings,
            tools=self.tools,
        )

    def _setup_tools(self) -> List[Tool]:
        """Setup Function Calling tools for Gemini using the unified registry"""
        try:
            registry = self._tool_registry
            all_tools = registry.get_all()
            if not all_tools:
                return []

            # GeminiAdapter で ToolDefinition → FunctionDeclaration に変換
            declarations = GeminiAdapter.convert_all(all_tools)
            function_declarations = [
                FunctionDeclaration(
                    name=d["name"],
                    description=d["description"],
                    parameters=d["parameters"],
                )
                for d in declarations
            ]

            return [Tool(function_declarations=function_declarations)]

        except Exception as e:
            logger.warning(
                "Gemini tool initialization was skipped: %s",
                safe_startup_reason(e),
            )
            return []

    def _build_system_prompt(self) -> str:
        """Build system prompt based on character configuration"""
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if suppress_automatic_context:
            return AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        return build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            custom_instructions=(
                None
                if suppress_automatic_context
                else get_user_custom_instructions_sync(self._get_session_user_id())
            ),
            include_static_tool_reference=False,
            # Gemini receives function declarations via the API.  Keep the
            # legacy textual tool-call syntax out of its system prompt.
            tool_protocol="native",
        )

    def _build_effective_system_prompt(self, story_context=None) -> str:
        turn = get_turn_context()
        suppress_automatic_context = bool(
            getattr(turn, "suppress_automatic_context", False)
        )
        # ``suppress_automatic_context`` is shared by Help and trusted
        # background controllers.  Help has no strict scope and must keep its
        # fixed, tool-free prompt; a Project Steward/agent-check turn carries a
        # server-issued strict scope and must retain its isolated prompt.
        strict_background_scope = bool(
            getattr(turn, "strict_project_scope", False)
        ) or get_strict_tool_allowlist() is not None
        command_capabilities = {
            str(value).strip().casefold()
            for value in (
                getattr(self, "current_command_capabilities", ()) or ()
            )
            if str(value).strip()
        }
        is_help_turn = "aoitalk_help" in command_capabilities
        if suppress_automatic_context and (
            is_help_turn or not strict_background_scope
        ):
            return AOITALK_HELP_ISOLATED_SYSTEM_PROMPT
        override = str(
            getattr(self, "_isolated_system_prompt_override", "") or ""
        ).strip()
        if override:
            return override
        # Keep compatibility with lightweight clients that still expose the
        # legacy private override attribute.
        override = str(getattr(self, "_system_prompt_override", "") or "").strip()
        if override:
            return override
        if story_context:
            return story_context.prompt
        return self._build_system_prompt()

    def get_generation_metadata(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        context_snapshots = list(getattr(self, "_last_context_snapshots", None) or [])
        if context_snapshots:
            bounded_snapshot = sanitized_snapshot_series(context_snapshots)
            if bounded_snapshot:
                metadata["context_snapshot"] = bounded_snapshot
        manifest = context_manifest_metadata(
            self,
            allow_snapshot_only=True,
        )
        if manifest is not None:
            metadata["context_manifest"] = manifest
        return metadata

    @staticmethod
    def _snapshot_part_text(part: Any) -> str:
        if isinstance(part, str):
            return part
        text = getattr(part, "text", None)
        if text:
            return str(text)
        function_response = getattr(part, "function_response", None)
        if function_response:
            return str(function_response)
        function_call = getattr(part, "function_call", None)
        if function_call:
            return str(function_call)
        return ""

    @classmethod
    def _snapshot_message_content(cls, parts: Any) -> Any:
        if isinstance(parts, (str, bytes)) or not hasattr(parts, "__iter__"):
            parts = [parts]
        else:
            parts = list(parts)
        text_parts = [text for part in parts if (text := cls._snapshot_part_text(part))]
        return "\n".join(text_parts)

    @staticmethod
    def _snapshot_has_part_field(parts: Any, field: str) -> bool:
        if isinstance(parts, (str, bytes)) or not hasattr(parts, "__iter__"):
            parts = [parts]
        for part in parts:
            if not isinstance(part, str) and getattr(part, field, None):
                return True
        return False

    def _capture_context_request(
        self,
        *,
        history: List[Any],
        latest_message: Any,
        tools: List[Tool],
        response_tokens: Optional[int],
        request_kind: str,
        model_name: Optional[str] = None,
    ) -> None:
        """Record the exact prompt layers used for one Gemini request."""
        try:
            # Keep ContextManifest traces subject to the same immutable
            # Project Context OFF boundary as the model prompt.
            rendered_bundle, bundle_parts = context_bundle_components(
                self._context_bundle_for_turn(
                    project_context_enabled_for_client(self)
                )
            )
            normalized_messages: List[Dict[str, Any]] = []
            for index, item in enumerate(history):
                if isinstance(item, dict):
                    role = str(item.get("role") or "unknown")
                    item_parts = item.get("parts", [])
                else:
                    role = str(getattr(item, "role", None) or "unknown")
                    item_parts = getattr(item, "parts", [])
                if self._snapshot_has_part_field(item_parts, "function_response"):
                    role = "tool"
                normalized_messages.append(
                    {
                        "role": "system" if index == 0 else role,
                        "content": self._snapshot_message_content(item_parts),
                    }
                )
            normalized_messages.append(
                {
                    "role": (
                        "tool"
                        if self._snapshot_has_part_field(
                            latest_message, "function_response"
                        )
                        else "user"
                    ),
                    "content": self._snapshot_message_content(latest_message),
                }
            )

            parts = [
                *message_components(without_text(normalized_messages, rendered_bundle)),
                *bundle_parts,
            ]
            if self._snapshot_has_part_field(latest_message, "inline_data"):
                parts.append(
                    component(
                        "attachments",
                        "添付ファイル・画像由来の入力",
                        source="Gemini send_message inline_data",
                        measurement="unavailable",
                        preview="画像入力（バイナリは保存しません）",
                    )
                )

            for tool in tools:
                for declaration in list(
                    getattr(tool, "function_declarations", []) or []
                ):
                    parts.append(
                        component(
                            "native_tool_schemas",
                            "Native tool schemas",
                            str(declaration),
                            source="Gemini GenerativeModel tools",
                            preview=str(
                                getattr(declaration, "name", "") or "tool schema"
                            ),
                        )
                    )

            context_snapshots = list(
                getattr(self, "_last_context_snapshots", None) or []
            )
            request_index = int(getattr(self, "_context_request_index", 0))
            self._context_request_index = request_index + 1
            context_snapshots.append(
                snapshot(
                    provider="gemini",
                    model=model_name or self.model_name,
                    components=parts,
                    response_tokens=response_tokens,
                    request_index=request_index,
                    request_kind=request_kind,
                )
            )
            self._last_context_snapshots = context_snapshots
        except Exception as exc:
            print(
                "[GeminiLLMClient] Context snapshot failed; "
                f"continuing without observation: {exc}"
            )

    @staticmethod
    def _gemini_usage_payload(response: Any) -> Optional[Dict[str, Any]]:
        """Gemini の usage_metadata を record_usage が扱える形に変換する。"""
        usage = getattr(response, "usage_metadata", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage_metadata")
        if usage is None:
            return None

        def _count(name: str) -> Optional[int]:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is None:
                return None
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return None

        input_tokens = _count("prompt_token_count")
        output_tokens = _count("candidates_token_count")
        if input_tokens is None and output_tokens is None:
            return None
        cached_tokens = _count("cached_content_token_count") or 0
        reasoning_tokens = _count("thoughts_token_count") or 0
        payload: Dict[str, Any] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cached_tokens": cached_tokens,
            "cache_read_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_provider": "gemini",
            "metrics_source": "gemini.usage_metadata",
        }
        resolved_model = getattr(response, "model_version", None)
        if resolved_model is None and isinstance(response, dict):
            resolved_model = response.get("model_version")
        if resolved_model:
            payload["resolved_model"] = str(resolved_model)
        return payload

    def _mark_usage_recorded(self, response: Any) -> bool:
        """同一レスポンスの二重記録を防ぐ。既に記録済みなら True を返す。"""
        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:  # noqa: BLE001
            # __slots__ 等で属性を持てないレスポンスは、直近のオブジェクトを
            # 強参照で保持して同一性で判定する（id の再利用を避けるため）。
            pass
        recorded = getattr(self, "_recorded_usage_responses", None)
        if recorded is None:
            recorded = []
            self._recorded_usage_responses = recorded
        if any(item is response for item in recorded):
            return True
        recorded.append(response)
        del recorded[:-8]
        return False

    def _record_gemini_usage(
        self,
        response: Any,
        *,
        model_name: Optional[str] = None,
        request_type: str = "chat",
        is_streaming: bool = False,
    ) -> None:
        """Gemini レスポンスの usage を永続化する（失敗しても本処理は落とさない）。"""
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            # Help is a one-turn Guide projection; do not create ordinary
            # token-usage records from a direct provider invocation.
            return
        try:
            payload = self._gemini_usage_payload(response)
            if not payload:
                return
            if self._mark_usage_recorded(response):
                return
            persist_usage_sync(
                self,
                provider="gemini",
                model=str(model_name or self.model_name),
                usage=payload,
                request_type=request_type,
                is_streaming=bool(is_streaming),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[GeminiLLMClient] usage記録に失敗しました: {exc}")

    def _reconcile_context_usage(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", None)
            if input_tokens is None and isinstance(usage, dict):
                input_tokens = usage.get("prompt_token_count")
            context_snapshots = list(
                getattr(self, "_last_context_snapshots", None) or []
            )
            if input_tokens is not None and context_snapshots:
                self._last_context_snapshots[-1] = reconcile_snapshot(
                    self._last_context_snapshots[-1],
                    int(input_tokens),
                )
        except Exception as exc:
            print(
                "[GeminiLLMClient] Context usage reconciliation failed; "
                f"keeping estimated snapshot: {exc}"
            )

    def _get_effective_gemini_tools(
        self,
        story_context: Any = _GEMINI_STORY_CONTEXT_UNSET,
    ) -> List[Tool]:
        # Gemini must consume the same request-local ToolDefinitions as the
        # provider-neutral exposure layer.  Rebuilding declarations from the
        # filtered registry updates delegate roster text and loader enum/schema
        # when a fixed client switches App/Story context.
        from .tool_exposure import (
            REVIEW_TOOL_ALLOWLIST,
            effective_tool_pack_session,
            filtered_registry_for_client,
            resolve_tool_exposure_context,
        )
        from .tool_packs import tool_visible_for_session

        # Trusted controller turns (notably ``/help``) provide their own
        # bounded prompt and must not inherit StoryWritingSession context from
        # a reused provider instance.  Force an explicit ``None`` through the
        # exposure layer so it cannot re-resolve the current session behind
        # the caller's back.
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if suppress_automatic_context:
            # Help is a tool-free controller turn.  Do not even expose the
            # legacy prebuilt declarations when a lightweight client has no
            # common registry to filter.
            return []

        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            # Lightweight test/legacy clients may only provide prebuilt Gemini
            # declarations.  Keep their historical filtering path; real
            # clients always use the common filtered ToolDefinition registry
            # below so descriptions and schemas are request-local.
            exposure = resolve_tool_exposure_context(
                self,
                story_context=(
                    None
                    if story_context is _GEMINI_STORY_CONTEXT_UNSET
                    else story_context
                ),
            )
            if exposure.story_resolution_failed:
                return []
            review_mode = (
                get_client_generation_policy(self).profile
                == GenerationProfile.REVIEW
            )
            session = effective_tool_pack_session(self, exposure=exposure)
            fallback_tools: List[Tool] = []
            for tool in self.tools:
                kept = []
                for declaration in list(
                    getattr(tool, "function_declarations", []) or []
                ):
                    name = str(getattr(declaration, "name", ""))
                    if review_mode and name not in REVIEW_TOOL_ALLOWLIST:
                        continue
                    if exposure.story_context and not is_story_workflow_tool_allowed(
                        name,
                        exposure.story_context,
                    ):
                        continue
                    if not review_mode and not tool_visible_for_session(
                        session,
                        name,
                        client=self,
                        contextual_scope=exposure.scope,
                    ):
                        continue
                    kept.append(declaration)
                if kept:
                    fallback_tools.append(Tool(function_declarations=kept))
            return fallback_tools

        if suppress_automatic_context:
            effective_registry = filtered_registry_for_client(
                self,
                registry,
                story_context=None,
            )
        elif story_context is _GEMINI_STORY_CONTEXT_UNSET or story_context is None:
            effective_registry = filtered_registry_for_client(
                self,
                registry,
            )
        else:
            effective_registry = filtered_registry_for_client(
                self,
                registry,
                # A non-None context was already resolved by the caller; pass
                # it through so this declaration rebuild stays request-local.
                story_context=story_context,
            )
        all_tools = effective_registry.get_all()
        if not all_tools:
            return []
        declarations = GeminiAdapter.convert_all(all_tools)
        function_declarations = [
            FunctionDeclaration(
                name=item["name"],
                description=item["description"],
                parameters=item["parameters"],
            )
            for item in declarations
        ]
        return [Tool(function_declarations=function_declarations)]

    def _model_for_effective_tools(
        self,
        effective_tools: List[Tool],
        *,
        model_name: Optional[str] = None,
        force_isolated: bool = False,
    ):
        """Build a model whose actual declarations match the selected purpose."""
        target_model = model_name or self.model_name
        if (
            not force_isolated
            and target_model == self.model_name
            and _same_function_declarations(
                effective_tools,
                getattr(self, "tools", None),
            )
        ):
            return self.model
        return genai.GenerativeModel(
            model_name=target_model,
            safety_settings=(
                self.model._safety_settings
                if hasattr(self.model, "_safety_settings")
                else None
            ),
            tools=effective_tools if effective_tools else None,
            system_instruction=(
                self.model._system_instruction
                if hasattr(self.model, "_system_instruction")
                else None
            ),
        )

    def transcribe_audio(self, file_path) -> Optional[str]:
        """Transcribe audio file to text using Gemini

        Args:
            file_path: Path to audio file (str or Path)

        Returns:
            Transcribed text or None on error
        """
        try:
            from pathlib import Path
            from ..services.turn_context import get_turn_context

            try:
                turn = get_turn_context()
            except Exception:
                turn = None
            inherited = get_privacy_policy_context()
            privacy_session_id = str(
                self.current_session_id or getattr(turn, "session_id", None) or ""
            )
            client_user_id = self._get_session_user_id()
            if not client_user_id or client_user_id == "default_user":
                client_user_id = getattr(turn, "user_id", None)
            privacy_user_id = str(client_user_id or "")
            privacy_session_context = (
                dict(inherited.session_context)
                if inherited.session_context is not None
                else (
                    dict(self._privacy_session_context)
                    if isinstance(self._privacy_session_context, dict)
                    else {}
                )
            )
            privacy_project_metadata = (
                dict(inherited.project_metadata)
                if inherited.project_metadata is not None
                else (
                    dict(self._privacy_project_metadata)
                    if isinstance(self._privacy_project_metadata, dict)
                    else {}
                )
            )
            self._privacy_session_context = dict(privacy_session_context)
            self._privacy_project_metadata = dict(privacy_project_metadata)
            if not isinstance(self._privacy_gateway, OutboundPrivacyGateway) or (
                self._privacy_gateway.user_id != privacy_user_id
                or self._privacy_gateway.session_id != privacy_session_id
            ):
                self._privacy_gateway = OutboundPrivacyGateway(
                    self.config,
                    user_id=privacy_user_id,
                    session_id=privacy_session_id,
                    session_context=privacy_session_context,
                    project_metadata=privacy_project_metadata,
                )
            else:
                self._privacy_gateway.update_policy_context(
                    session_context=privacy_session_context,
                    project_metadata=privacy_project_metadata,
                )

            # Convert to Path if needed
            if isinstance(file_path, str):
                file_path = Path(file_path)

            print(f"[GeminiLLMClient] Transcribing audio: {file_path}")

            prompt = "Please transcribe the speech in this audio file. Output only the transcribed text without any additional explanation."
            media_bytes = file_path.read_bytes()
            media_type = mimetypes.guess_type(str(file_path))[0] or "audio/wav"
            descriptor = EgressDescriptor(
                action="model.transcribe",
                transport="gemini.files.upload+generate_content",
                destination="https://generativelanguage.googleapis.com",
                provider="gemini",
                model="gemini-2.5-flash",
            )

            def send_audio_request(outbound: dict[str, Any]) -> Any:
                # Build the upload and transcription request only from the
                # final payload supplied by the gateway.  BytesIO avoids
                # reintroducing a local filesystem path after protected
                # redaction and keeps both provider calls in one transaction.
                outbound_prompt = outbound.get("prompt")
                if not isinstance(outbound_prompt, str) or not outbound_prompt:
                    raise PrivacyError("Gemini audio prompt is missing")
                outbound_media = outbound.get("media")
                if isinstance(outbound_media, (bytes, bytearray, memoryview)):
                    upload_source: Any = io.BytesIO(bytes(outbound_media))
                else:
                    upload_source = outbound_media
                audio_file = genai.upload_file(
                    path=upload_source,
                    mime_type=media_type,
                )
                transcription_model = genai.GenerativeModel("gemini-2.5-flash")
                return transcription_model.generate_content(
                    [outbound_prompt, audio_file]
                )

            response = self._privacy_gateway.execute_sync(
                {"prompt": prompt, "media": media_bytes},
                provider="gemini",
                descriptor=descriptor,
                sender=send_audio_request,
                source_kind="audio_transcription",
                model="gemini-2.5-flash",
            )
            # Audio transcription is a direct Gemini API request outside the
            # normal chat loop.  Record it independently when usage metadata
            # is returned; ``_record_gemini_usage`` safely no-ops otherwise.
            self._record_gemini_usage(
                response,
                model_name="gemini-2.5-flash",
                request_type="stt",
            )

            # Extract text
            transcription = self._privacy_gateway.restore(str(response.text or "").strip())

            print(
                f"[GeminiLLMClient] Transcription successful: {len(transcription)} chars"
            )
            return transcription

        except Exception as e:
            print(f"[GeminiLLMClient] Transcription failed: {e}")
            import traceback

            traceback.print_exc()
            return None

    def set_character(self, character_name: str):
        """Set character and update system prompt

        Args:
            character_name: Name of the character
        """
        self.character_name = character_name
        self._isolated_system_prompt_override = ""
        self.system_prompt = self._build_system_prompt()

    def set_session_context(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ):
        """Update identifiers used for persistent memory logging."""
        self._isolated_system_prompt_override = ""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}
            if "privacy_mode" in metadata:
                self._privacy_session_context["privacy_mode"] = str(
                    metadata.get("privacy_mode") or ""
                )

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def _get_memory_metadata(self) -> Dict[str, Any]:
        metadata = self.session_metadata.copy() if self.session_metadata else {}
        manifest = context_manifest_metadata(self)
        if manifest is not None:
            metadata["context_manifest"] = manifest
        return metadata

    def _get_user_memory_metadata(self) -> Dict[str, Any]:
        metadata = self.session_metadata.copy() if self.session_metadata else {}
        metadata.pop("context_manifest", None)
        return metadata

    def update_character(self, yaml_filename: str):
        """Update character from YAML file

        Args:
            yaml_filename: YAML filename (without extension)
        """
        # Load character configuration from YAML
        if self.config:
            new_config = self.config.get_character_config(yaml_filename)
            if new_config:
                self.character_name = new_config.get("name", yaml_filename)
                self._isolated_system_prompt_override = ""
                # Clear conversation history when switching characters
                self.clear_history()
                self.system_prompt = self._build_system_prompt()
                print(
                    f"[GeminiLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)"
                )
            else:
                print(
                    f"[GeminiLLMClient] キャラクター設定が見つかりません: {yaml_filename}"
                )
        else:
            print(f"[GeminiLLMClient] 設定オブジェクトがありません")

    def set_system_prompt(self, prompt: str):
        """Set custom system prompt

        Args:
            prompt: System prompt
        """
        self._isolated_system_prompt_override = ""
        self.system_prompt = prompt
        logger.info(
            "Gemini system prompt configured",
            extra=FILE_ONLY_LOG_EXTRA,
        )

    def set_isolated_system_prompt(self, prompt: str):
        isolated_prompt = str(prompt or "").strip()
        self._isolated_system_prompt_override = isolated_prompt
        self.system_prompt = isolated_prompt

    def set_llm_mode(self, mode: str):
        """Set LLM response mode

        Args:
            mode: 'fast' for quick responses, 'thinking' for deeper reasoning

        Note: For Gemini 2.5/3.0, thinking mode uses thinking_config parameter
        """
        if mode not in ["fast", "thinking"]:
            print(f"[GeminiLLMClient] Invalid mode '{mode}', defaulting to 'fast'")
            mode = "fast"

        self._thinking_mode = mode == "thinking"
        print(f"[GeminiLLMClient] LLM mode set to: {mode}")

    def get_llm_mode(self) -> str:
        """Get current LLM response mode

        Returns:
            Current mode ('fast' or 'thinking')
        """
        return "thinking" if getattr(self, "_thinking_mode", False) else "fast"

    @staticmethod
    def _approved_action_directive():
        """Resolve the provider-neutral approved-plan cursor lazily."""

        from ..services.planning_runtime import get_approved_action_directive

        return get_approved_action_directive()

    @staticmethod
    def _current_planning_state():
        from .planning_policy import get_current_planning_run_state

        return get_current_planning_run_state()

    @staticmethod
    def _approved_action_budget(state: Any, tool_call_count: int) -> int | None:
        """Snapshot the provider-round budget for an executing approved plan."""

        if (
            state is None
            or not getattr(state, "plan", None)
            or str(getattr(state, "phase", ""))
            not in {"executing", "PlanningRunPhase.EXECUTING"}
        ):
            return None
        actions = tuple(state.plan.actions or ())
        cursor = int(
            (getattr(state, "metadata", {}) or {}).get(
                "approved_action_cursor", 0
            )
        )
        if cursor < 0 or cursor > len(actions):
            raise ValueError("approved action cursor out of range")
        # Include rounds already consumed before approval, every action still
        # pending at the server cursor, and one mandatory no-tools final.
        return int(tool_call_count) + (len(actions) - cursor) + 1

    def _approved_action_call(self, directive, *, tool_name: str, arguments: Any):
        """Bind a Gemini function call to one server-owned action."""

        from ..services.planning_runtime import bind_approved_action_call
        from ..services.turn_context import override_turn_context, reset_turn_context

        token = override_turn_context(tool_call_id=directive.call_id)
        try:
            return bind_approved_action_call(
                directive,
                tool_name=tool_name,
                proposed_arguments=_plain_json_object(arguments),
            )
        finally:
            reset_turn_context(token)

    def _approved_action_receipt(self, directive):
        from ..services.planning_runtime import get_approved_action_receipt

        return self._run_async_sync(get_approved_action_receipt(directive))

    def _accept_approved_action_receipt(self, directive, receipt):
        from ..services.planning_runtime import accept_approved_action_receipt

        return self._run_async_sync(accept_approved_action_receipt(directive, receipt))

    def _persist_approved_action_result(
        self,
        directive,
        *,
        arguments: dict[str, Any],
        result: Any,
        success: bool,
    ):
        from ..services.planning_runtime import persist_and_accept_approved_action_result

        return self._run_async_sync(
            persist_and_accept_approved_action_result(
                directive,
                arguments=arguments,
                result=result,
                success=bool(success),
            )
        )

    def _fail_approved_action(self, directive, *, reason: str, detail: str = ""):
        from ..services.planning_runtime import fail_approved_action

        return self._run_async_sync(
            fail_approved_action(directive, reason=reason, detail=detail)
        )

    def _protect_approved_action_replay(
        self,
        directive: Any,
        receipt_output: str,
    ) -> tuple[dict[str, Any], str]:
        """Protect durable replay payloads before they re-enter Gemini.

        Receipt arguments are durable execution evidence, but the synthetic
        ``FunctionCall`` sent during a reconnect must be reconstructed from
        the current server-owned directive rather than trusting provider
        payloads stored in the receipt.  Arguments and result are transformed
        together so one gateway alias table is used for the whole replay.
        """

        try:
            directive_arguments = _plain_json_object(
                getattr(directive, "arguments", None) or {}
            )
            protected = self._privacy_gateway.protect_sync(
                {
                    "arguments": directive_arguments,
                    "result": str(receipt_output),
                },
                provider="gemini",
                source_kind="approved_action_replay",
            )
            protected_payload = getattr(protected, "payload", None)
            if not isinstance(protected_payload, Mapping):
                raise ValueError("protected replay payload must be an object")
            protected_arguments = _plain_json_object(
                protected_payload.get("arguments")
            )
            protected_result = protected_payload.get("result")
            if not isinstance(protected_result, str):
                raise ValueError("protected replay result must be a string")
            # Validate the exact values that will cross the provider boundary,
            # not merely the pre-transform directive.  ``_plain_json_object``
            # removes Gemini proto containers from arguments; JSON encoding
            # then rejects any remaining provider-specific object instead of
            # allowing an implicit stringification fallback.
            json.dumps(
                protected_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            json.dumps(protected_result, ensure_ascii=False)
            return protected_arguments, protected_result
        except Exception as exc:
            self._fail_approved_action(
                directive,
                reason="approved_action_privacy_protection_failed",
                detail=str(exc),
            )
            raise AssertionError("unreachable")

    def _fail_completed_plan_provider_call(
        self,
        function_calls: Sequence[Any],
    ) -> None:
        """Fail closed when Gemini emits tools after plan execution completed.

        A completed approved plan is requested with ``tools=[]`` and
        ``function_calling_config.mode=NONE``.  Providers can still return a
        malformed/stale function-call part, however.  There is no current
        approved action to attach to such a call, so use the planning
        runtime's plan-level failure sentinel instead of manufacturing an
        ``action.failed`` receipt with a null action.
        """

        from ..services.planning_runtime import (
            _planning_failure,
            _record_planning_audit_event,
        )

        # Keep a plan-level audit trail without persisting provider arguments
        # or pretending that a non-existent approved action failed.
        try:
            self._run_async_sync(
                _record_planning_audit_event(
                    "plan.execution.failed",
                    status="failed",
                    payload={
                        "reason": "approved_plan_completed_tool_call",
                        "provider_function_count": len(function_calls),
                    },
                )
            )
        except Exception:
            logger.debug(
                "Failed to record completed-plan provider tool violation",
                exc_info=True,
            )
        raise _planning_failure("approved_plan_completed_tool_call")

    @staticmethod
    def _gemini_tool_config(*, mode: str, allowed_function_names: Sequence[str] = ()) -> dict[str, Any]:
        config: dict[str, Any] = {"mode": str(mode).upper()}
        names = [str(name).strip() for name in allowed_function_names if str(name).strip()]
        if names and config["mode"] == "ANY":
            config["allowed_function_names"] = names
        return {"function_calling_config": config}

    @staticmethod
    def _filter_gemini_tools(tools: Sequence[Any], tool_name: str) -> list[Any]:
        """Keep exactly one declaration for an approved action round."""

        filtered: list[Any] = []
        target = str(tool_name or "").strip()
        for tool in list(tools or []):
            declarations = [
                declaration
                for declaration in list(
                    getattr(tool, "function_declarations", []) or []
                )
                if str(getattr(declaration, "name", "") or "") == target
            ]
            if declarations:
                filtered.append(Tool(function_declarations=declarations))
        return filtered

    @staticmethod
    def _function_response_part(name: str, result: Any) -> Any:
        """Build a Gemini function response using plain JSON payloads."""

        return genai.protos.Part(
            function_response=genai.protos.FunctionResponse(
                name=str(name or ""),
                response={"result": str(result or "")},
            )
        )

    @staticmethod
    def _synthetic_function_call_part(name: str, arguments: Mapping[str, Any]) -> Any:
        return genai.protos.Part(
            function_call=genai.protos.FunctionCall(
                name=str(name or ""),
                args=_plain_json_object(arguments),
            )
        )

    def _execute_tool_result(
        self,
        function_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        call_id: str = "",
    ) -> UnifiedToolResult:
        """Execute through the unified router without serializing control flow."""

        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        story_context = (
            None if suppress_automatic_context else self._get_story_chat_context_sync()
        )
        if story_context and not is_story_workflow_tool_allowed(
            function_name,
            story_context,
        ):
            call = UnifiedToolCall(
                tool=function_name,
                arguments=_plain_json_object(arguments or {}),
                call_id=call_id,
            )
            return UnifiedToolResult(
                call=call,
                output=(
                    f"{function_name} is not available in this scenario "
                    f"{story_context.mode} session. Continue using the scenario "
                    "workflow context only."
                ),
                success=False,
                error="tool unavailable in scenario",
            )

        registry = filtered_registry_for_client(
            self,
            self._tool_registry,
            **({"story_context": None} if suppress_automatic_context else {}),
        )
        if function_name not in registry:
            call = UnifiedToolCall(
                tool=function_name,
                arguments=_plain_json_object(arguments or {}),
                call_id=call_id,
            )
            return UnifiedToolResult(
                call=call,
                output=f"エラー: 未知の関数 '{function_name}'",
                success=False,
                error="unknown tool",
            )

        return RegistryToolRouter(
            registry,
            log_prefix="GeminiLLMClient",
            config=self.config,
            client=self,
            user_input="",
        ).execute(
            UnifiedToolCall(
                tool=function_name,
                arguments=_plain_json_object(arguments or {}),
                call_id=call_id,
            )
        )

    def _execute_tool(self, function_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool function and return its result"""
        try:
            arguments = _plain_json_object(arguments or {})
            print(f"[GeminiLLMClient] ツール実行: {function_name} with {arguments}")

            suppress_automatic_context = bool(
                getattr(get_turn_context(), "suppress_automatic_context", False)
            )
            if suppress_automatic_context:
                # This legacy raw seam can be called directly by lightweight
                # integrations even though the normal Gemini loop exposes no
                # tools for Help.  Keep the controller boundary fail-closed so
                # a model/provider cannot smuggle an arbitrary mutation here.
                return "エラー: AoiTalk Helpターンではツールを実行できません"
            story_context = (
                self._get_story_chat_context_sync()
            )
            if story_context and not is_story_workflow_tool_allowed(
                function_name,
                story_context,
            ):
                return (
                    f"{function_name} is not available in this scenario "
                    f"{story_context.mode} session. Continue using the scenario "
                    "workflow context only."
                )

            # 統一レジストリからツール取得・実行
            registry = filtered_registry_for_client(
                self,
                self._tool_registry,
                **({"story_context": None} if suppress_automatic_context else {}),
            )
            if function_name not in registry:
                return f"エラー: 未知の関数 '{function_name}'"

            try:
                result = RegistryToolRouter(
                    registry,
                    log_prefix="GeminiLLMClient",
                    config=self.config,
                ).execute(
                    UnifiedToolCall(
                        tool=function_name,
                        arguments=dict(arguments or {}),
                    )
                )

                print(f"[GeminiLLMClient] ツール結果: {result.model_output}")
                return str(result.model_output)

            except PlanningInteractionTerminated:
                raise
            except Exception as e:
                error_msg = f"ツール実行エラー ({function_name}): {str(e)}"
                print(f"[GeminiLLMClient] {error_msg}")
                return error_msg

        except PlanningInteractionTerminated:
            # Planning timeout/cancel and approval failures are control-flow
            # signals consumed by the terminal mode; never stringify them as
            # a provider fallback or model-visible tool result.
            raise
        except Exception as e:
            error_msg = f"ツール実行エラー ({function_name}): {str(e)}"
            print(f"[GeminiLLMClient] {error_msg}")
            import traceback

            traceback.print_exc()
            return error_msg

    def _build_conversation_context(self, user_input: str) -> List[Dict[str, str]]:
        """Build conversation context from history for Gemini chat"""
        messages = []

        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        story_context = (
            None if suppress_automatic_context else self._get_story_chat_context_sync()
        )
        # Scenario workflow sessions use a dedicated system prompt instead of
        # the globally selected app-header assistant prompt.
        enhanced_system_prompt = self._build_effective_system_prompt(story_context)
        isolated_override = str(
            getattr(self, "_isolated_system_prompt_override", "") or ""
        ).strip()
        include_project_context = project_context_enabled_for_client(self)
        context_bundle = self._context_bundle_for_turn(include_project_context)
        context_builder_block = (
            context_bundle.render_for_prompt()
            if (
                not suppress_automatic_context
                and not isolated_override
                and not story_context
                and context_bundle
            )
            else ""
        )
        if context_builder_block:
            enhanced_system_prompt = (
                f"{enhanced_system_prompt}\n\n{context_builder_block}"
            )
        project_context = (
            None
            if (
                suppress_automatic_context
                or isolated_override
                or story_context
                or not include_project_context
            )
            else get_runtime_project_context()
        )
        if project_context and not context_builder_block:
            project_block = format_project_context_for_chat_prompt(project_context)
            if project_block:
                enhanced_system_prompt = f"{enhanced_system_prompt}\n\n{project_block}"

        # TRPG play state injection was removed with the deprecated /play path.

        # Add enhanced system prompt as the first user message
        messages.append({"role": "user", "parts": [enhanced_system_prompt]})
        messages.append(
            {"role": "model", "parts": ["了解しました。設定と記憶を理解しました。"]}
        )

        # Add conversation history (last 10 exchanges).  Isolated controller
        # turns (notably Help) must remain one-turn even if this helper is
        # called outside ``generate_response`` or a stale client history is
        # still present.
        if not suppress_automatic_context and self.conversation_history:
            for msg in self.conversation_history[
                -20:
            ]:  # Last 10 exchanges (user + assistant)
                if msg["role"] == "user":
                    messages.append({"role": "user", "parts": [msg["content"]]})
                elif msg["role"] == "assistant":
                    messages.append({"role": "model", "parts": [msg["content"]]})

        # Add current user input
        messages.append({"role": "user", "parts": [user_input]})

        return messages

    def _context_bundle_for_turn(
        self,
        include_project_context: bool,
    ) -> ContextBundle | None:
        """Hide stale selected-Project layers when Project Context is OFF."""

        bundle = self._current_context_bundle
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if bundle is None or include_project_context:
            return bundle
        return strip_project_context_bundle(bundle)

    def _get_routed_model(self, user_input: str) -> Optional[str]:
        return None

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if not self.current_project_id and not self.current_session_id:
            return None

        if self._get_story_chat_context_sync():
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_sync(
                resolver.resolve_context(
                    project_id=self.current_project_id,
                    session_id=self.current_session_id,
                    user_id=self._get_session_user_id(),
                )
            )
        except Exception as e:
            print(f"[GeminiLLMClient] Failed to resolve project context: {e}")
            return None

    def _get_story_chat_context_sync(self):
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if not self.current_session_id:
            return None
        return run_story_chat_context_sync(
            self._run_async_sync,
            self.current_session_id,
        )

    def _run_async_sync(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _build_context_bundle_sync(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if bool(getattr(get_turn_context(), "suppress_automatic_context", False)):
            return None
        if self._get_story_chat_context_sync():
            return None
        try:
            try:
                context_builder = ContextBuilder(manifest_config=self.config)
            except TypeError:
                context_builder = ContextBuilder()
            return self._run_async_sync(
                context_builder.build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id,
                    task_id=get_turn_context().task_id,
                    session_id=self.current_session_id,
                    project_context=project_context,
                    include_project_context=project_context_enabled_for_client(self),
                )
            )
        except Exception as e:
            print(
                f"[GeminiLLMClient] ContextBuilder failed; fallback to basic context: {e}"
            )
            return None

    def _build_tool_hint_context(self, user_input: str) -> str:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if suppress_automatic_context:
            return ""
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=filtered_registry_for_client(
                self,
                self._tool_registry,
                **({"story_context": None} if suppress_automatic_context else {}),
            ),
            policy=get_client_generation_policy(self),
            log_prefix="GeminiLLMClient",
        )

    def _render_gemini_context_for_review(
        self,
        context: List[Dict[str, Any]],
        latest_user_message: Any,
    ) -> str:
        messages: list[dict[str, Any]] = []
        for item in context[:-1]:
            parts = item.get("parts", [])
            if isinstance(parts, list):
                content = "\n".join(str(part) for part in parts)
            else:
                content = str(parts)
            messages.append({"role": item.get("role", "message"), "content": content})
        messages.append({"role": "user", "content": latest_user_message})
        return render_messages_for_review(messages)

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        generation_config: Any,
        user_input: str,
        is_streaming: bool = False,
    ) -> str:
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if suppress_automatic_context:
            raise RuntimeError("Gemini review loop is disabled for isolated Help turns")
        story_context = (
            None if suppress_automatic_context else self._get_story_chat_context_sync()
        )
        effective_tools = self._get_effective_gemini_tools(story_context)
        review_model = self._model_for_effective_tools(
            effective_tools,
            force_isolated=bool(story_context) or is_review_generation(self),
        )
        chat = review_model.start_chat(history=[])
        latest_message: Any = prompt
        tool_calls: list[OpenAIToolCallRecord] = []

        for _ in range(5):
            self._capture_context_request(
                history=list(getattr(chat, "history", None) or []),
                latest_message=latest_message,
                tools=effective_tools,
                response_tokens=getattr(generation_config, "max_output_tokens", None),
                request_kind="agentic_review",
            )
            response = self._send_message_with_deadline(
                chat,
                latest_message,
                generation_config=generation_config,
            )
            self._reconcile_context_usage(response)
            self._record_gemini_usage(
                response,
                request_type="review",
                is_streaming=bool(is_streaming),
            )
            candidates = getattr(response, "candidates", []) or []
            if not candidates:
                return ""
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", []) if content else []
            function_calls = []
            text_parts = []
            for part in parts:
                if hasattr(part, "function_call") and part.function_call:
                    function_calls.append(part.function_call)
                elif (
                    hasattr(part, "text")
                    and part.text
                    and not _gemini_part_is_thought(part)
                ):
                    # thought part はレビュー本文へ混ぜない。
                    text_parts.append(part.text)

            if text_parts:
                return guard_tool_execution_claims("".join(text_parts), tool_calls)

            if not function_calls:
                return ""

            function_response_parts = []
            for func_call in function_calls:
                function_name = func_call.name
                arguments = _plain_json_object(
                    getattr(func_call, "args", None) or {},
                    path=f"review.{function_name}",
                )
                result = self._execute_tool(function_name, arguments)
                tool_calls.append(
                    OpenAIToolCallRecord(
                        tool=function_name,
                        arguments=arguments,
                        result=result,
                    )
                )
                function_response_parts.append(
                    self._function_response_part(function_name, result)
                )
            latest_message = function_response_parts

        return "ツール実行の上限に達したため、検証を完了できませんでした。"

    def _run_agentic_completion_from_valid_response(
        self,
        *,
        context: str,
        user_input: str,
        initial_response: str,
        generation_config: Any,
        is_streaming: bool,
    ) -> str:
        """Review a valid response without hiding continuation failures.

        A review is optional verification, so only its transport deadline may
        retain the already-valid provider response. A requested continuation
        is additional production work and keeps the provider's existing error
        propagation/fallback contract.
        """

        def run_continuation_once(prompt: str) -> str:
            return self._run_agentic_review_once(
                prompt,
                generation_config=generation_config,
                user_input=user_input,
                is_streaming=is_streaming,
            )

        def run_review_once(prompt: str) -> str:
            try:
                return run_continuation_once(prompt)
            except DeadlineExceeded as exc:
                raise _GeminiAgenticReviewDeadline from exc

        try:
            return run_agentic_completion_loop_sync(
                client=self,
                run_once=run_continuation_once,
                run_review_once=run_review_once,
                run_continuation_once=run_continuation_once,
                context=context,
                user_input=user_input,
                initial_response=initial_response,
            )
        except _GeminiAgenticReviewDeadline as review_timeout:
            print(
                "[GeminiLLMClient] agentic reviewがタイムアウト"
                "したため有効な応答を保持します: "
                f"{review_timeout.__cause__ or review_timeout}"
            )
            return initial_response

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> Union[str, Generator[str, None, None]]:
        """Generate response using Gemini with Function Calling and multimodal support

        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            stream: Whether to stream response
            image_data: Optional image data {data: base64 data URL, mimeType: str, name: str}
            stream_callback: Optional stream event callback
                (tool_start / tool_end / assistant_text / thinking)

        Returns:
            Generated response
        """
        turn_event_emitter = make_sync_stream_emitter(stream_callback)
        # Capture session_id locally to prevent race conditions with concurrent requests
        session_id = self.current_session_id
        self.current_assistant_message_id = None
        edit_message_id = self.current_edit_message_id
        external_persistence = bool(
            getattr(self, "external_persistence_enabled", False)
        )
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        # Trusted controller turns such as AoiTalk Help carry a complete,
        # bounded context of their own.  They must not read or append the
        # provider's durable conversation history, even when a long-lived
        # Gemini client is reused for a normal session.
        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        isolated_privacy_gateway_previous = (
            getattr(self, "_privacy_gateway", None)
            if suppress_automatic_context
            else None
        )
        isolated_privacy_gateway_active = bool(suppress_automatic_context)
        if isolated_privacy_gateway_active:
            # Direct Gemini callers may skip TerminalMode's provider snapshot.
            # Start with a fresh alias scope and restore the ordinary gateway
            # after this one-turn Guide request completes.
            try:
                turn = get_turn_context()
                self._privacy_gateway = OutboundPrivacyGateway(
                    getattr(self, "config", None),
                    user_id=str(
                        getattr(turn, "user_id", None)
                        or self._get_session_user_id()
                        or ""
                    ),
                    session_id=str(
                        getattr(turn, "session_id", None)
                        or session_id
                        or ""
                    ),
                    session_context={},
                    project_metadata={},
                )
                setattr(self._privacy_gateway, "_aoitalk_help_isolated", True)
            except Exception:
                isolated_privacy_gateway_active = False
        isolated_history_snapshot = None
        if suppress_automatic_context:
            # ``TerminalMode`` normally snapshots the whole provider before
            # priming Help.  Keep this direct-provider safeguard as well for
            # legacy callers that invoke Gemini without that orchestration.
            with self._history_lock:
                try:
                    saved_history = copy.deepcopy(self.conversation_history)
                except Exception:
                    saved_history = list(self.conversation_history or [])
                isolated_history_snapshot = (
                    saved_history,
                    getattr(self, "_loaded_session_id", None),
                )
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )

        try:
            # Alias mappings are session-scoped; never carry them to another
            # conversation when a long-lived Gemini client is reused.
            turn_context = get_turn_context()
            privacy_user_id = str(
                (
                    getattr(turn_context, "user_id", None) or ""
                    if suppress_automatic_context
                    else self._get_session_user_id() or ""
                )
            )
            privacy_session_id = str(
                (
                    getattr(turn_context, "session_id", None) or ""
                    if suppress_automatic_context
                    else session_id or ""
                )
            )
            privacy_session_context = (
                {}
                if suppress_automatic_context
                else (
                    self._privacy_session_context
                    if isinstance(self._privacy_session_context, dict)
                    else {}
                )
            )
            privacy_project_metadata = (
                {}
                if suppress_automatic_context
                else (
                    self._privacy_project_metadata
                    if isinstance(self._privacy_project_metadata, dict)
                    else {}
                )
            )
            gateway = getattr(self, "_privacy_gateway", None)
            if not isinstance(gateway, OutboundPrivacyGateway) or (
                gateway.session_id != privacy_session_id
                or gateway.user_id != privacy_user_id
            ):
                gateway = OutboundPrivacyGateway(
                    getattr(self, "config", None),
                    session_id=privacy_session_id,
                    user_id=privacy_user_id,
                    session_context=privacy_session_context,
                    project_metadata=privacy_project_metadata,
                )
                self._privacy_gateway = gateway
            else:
                # Explicit empty mappings clear a prior turn's scope rather
                # than retaining stale project/session policy on a singleton.
                gateway.update_policy_context(
                    session_context=privacy_session_context,
                    project_metadata=privacy_project_metadata,
                )
            self._last_context_snapshots = []
            self._context_request_index = 0
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            self._privacy_project_metadata = (
                dict((project_context or {}).get("metadata") or {})
                if isinstance(project_context, dict)
                and isinstance((project_context or {}).get("metadata"), dict)
                else {}
            )
            self._privacy_gateway.update_policy_context(
                session_context=privacy_session_context,
                project_metadata=(
                    privacy_project_metadata
                    if suppress_automatic_context
                    else (
                        self._privacy_project_metadata
                        if isinstance(self._privacy_project_metadata, dict)
                        else {}
                    )
                ),
            )
            self._current_context_bundle = self._build_context_bundle_sync(
                user_input, project_context
            )

            # Lock protects conversation_history and _loaded_session_id from concurrent access
            with self._history_lock:
                if suppress_automatic_context:
                    # Do not allow a previous normal turn's in-memory history
                    # (or a DB reload) to enter the Help prompt.  Reset the
                    # loaded marker so the next ordinary turn reloads the
                    # canonical session history instead of inheriting this
                    # intentionally empty provider context.
                    self.conversation_history = []
                    self._loaded_session_id = None
                # Load conversation history from database only when session changes
                elif session_id and self.memory_manager and self._memory_enabled:
                    if session_id != self._loaded_session_id:
                        try:
                            print(
                                f"[GeminiLLMClient] Loading history for new session: {session_id}"
                            )
                            messages = self._safe_memory_operation(
                                self._load_session_history, session_id
                            )
                            if messages is not None:
                                self.conversation_history = messages
                                self._loaded_session_id = session_id
                                print(
                                    f"[GeminiLLMClient] Loaded {len(messages)} messages"
                                )
                        except Exception as e:
                            print(
                                f"[GeminiLLMClient] Failed to load session history: {e}"
                            )

                # Build conversation context (reads conversation_history)
                context = self._build_conversation_context(user_input)

            model_user_input = user_input
            tool_hint_context = self._build_tool_hint_context(user_input)
            model_user_input = compose_tool_hint_user_message(
                user_input,
                tool_hint_context,
            )
            if tool_hint_context:
                if context:
                    context[-1]["parts"] = [model_user_input]

            # Initialize memory manager if needed and save user message (outside lock, fire-and-forget)
            if (
                not suppress_automatic_context
                and self.memory_manager
                and self._memory_enabled
                and not external_persistence
            ):
                try:
                    if session_id:
                        # Freeze user metadata before the background coroutine can
                        # observe a later turn. User messages never carry Manifest.
                        user_metadata = self._get_user_memory_metadata()
                        self._safe_memory_operation(
                            self._save_user_message_to_session,
                            user_input,
                            session_id,
                            edit_message_id,
                            user_metadata,
                            fire_and_forget=True,
                        )
                    # Note: If no session_id, we skip saving to avoid creating project_id=None sessions
                    # The session should be created by frontend via API call to /api/conversations
                except Exception as e:
                    print(
                        f"[GeminiLLMClient] Failed to save user message to memory: {e}"
                    )

            # Clear recent tool calls for new request
            self._recent_tool_calls = []

            # Apply mode-specific parameters
            thinking_mode = getattr(self, "_thinking_mode", False)

            # Adjust temperature based on mode
            if thinking_mode:
                # Thinking mode: lower temperature for more focused reasoning
                effective_temperature = 0.6
            else:
                # Fast mode: use provided temperature
                effective_temperature = temperature

            # Generate configuration
            generation_config_kwargs = {
                "temperature": effective_temperature,
                "max_output_tokens": max_tokens or (2048 if thinking_mode else 1024),
                "candidate_count": 1,
            }

            # Add thinking_config if in thinking mode (Gemini 2.5+ / 3.0+)
            # This will be tried first; if the model doesn't support it,
            # we'll catch the error and retry without thinking_config
            use_thinking_config = False
            if thinking_mode:
                try:
                    # Check if GenerationConfig supports thinking_config
                    import inspect

                    gen_config_params = inspect.signature(
                        genai.types.GenerationConfig
                    ).parameters
                    if "thinking_config" in gen_config_params:
                        generation_config_kwargs["thinking_config"] = {
                            "thinking_budget": 2048,  # Token budget for thinking
                            # thought part を受け取ってthinkingイベントとして配信する。
                            # 非対応SDK/サーバーでは後段で自動的に除外される。
                            "include_thoughts": True,
                        }
                        use_thinking_config = True
                        print(
                            f"[GeminiLLMClient] Thinking mode enabled with budget: 2048 tokens"
                        )
                    else:
                        print(
                            f"[GeminiLLMClient] Model doesn't support thinking_config, using standard mode with lower temperature"
                        )
                except Exception as e:
                    print(
                        f"[GeminiLLMClient] thinking_config check failed, using standard mode: {e}"
                    )

            generation_config, uses_thought_parts = _gemini_generation_config(
                generation_config_kwargs
            )

            # モデルルーティング: 有効なら動的モデル選択
            story_context = (
                None
                if suppress_automatic_context
                else self._get_story_chat_context_sync()
            )
            effective_tools = self._get_effective_gemini_tools(story_context)
            routed_model = self._get_routed_model(user_input)
            if routed_model and routed_model != self.model_name:
                print(
                    f"[GeminiLLMClient] モデルルーティング: {self.model_name} → {routed_model}"
                )
                routed_genai_model = self._model_for_effective_tools(
                    effective_tools,
                    model_name=routed_model,
                    force_isolated=True,
                )
                chat = routed_genai_model.start_chat(history=context[:-1])
            elif story_context or is_review_generation(self):
                purpose_model = self._model_for_effective_tools(
                    effective_tools,
                    force_isolated=True,
                )
                chat = purpose_model.start_chat(history=context[:-1])
            else:
                # 未ロード pack を除いた宣言でモデルを組み直す。絞り込みが
                # 発生しない場合は既定モデル（self.model）がそのまま返る。
                chat = self._model_for_effective_tools(
                    effective_tools
                ).start_chat(
                    history=context[:-1]
                )  # All except last message

            # Ordinary turns retain the historical five-round cap.  An
            # approved plan gets a separate budget snapshot once execution is
            # observed: already-consumed provider rounds, the actions still
            # pending at the server cursor, and one mandatory no-tools final
            # synthesis round.  Recomputing from the full plan on every loop
            # would lose pre-approval rounds and can truncate a valid plan.
            max_tool_calls = 5
            approved_round_budget: int | None = None
            tool_call_count = 0

            # Build the message content - handle multimodal input
            message_parts = []

            # Add images if provided.  The raw attachment is carried as an
            # auxiliary privacy payload into ``_send_message_with_deadline``;
            # that method executes the provider call only after the gateway
            # has reviewed the exact request (including media policy).
            for image_item in normalize_image_payloads(image_data):
                from google.generativeai import protos

                try:
                    mime_type, image_bytes = data_url_to_bytes(image_item["data"])
                    mime_type = image_item.get("mimeType") or mime_type or "image/jpeg"
                    image_part = protos.Part(
                        inline_data=protos.Blob(mime_type=mime_type, data=image_bytes)
                    )
                    message_parts.append(image_part)
                    print(
                        f"[GeminiLLMClient] 画像添付あり: {image_item.get('name', 'unknown')} ({mime_type}, {len(image_bytes)} bytes)"
                    )
                except Exception as img_error:
                    print(f"[GeminiLLMClient] 画像処理エラー: {img_error}")
                    import traceback

                    traceback.print_exc()

            # Add text if provided
            if user_input:
                message_parts.append(model_user_input)

            # Use the original context[-1]["parts"] or the new multimodal parts
            latest_message = message_parts if message_parts else context[-1]["parts"]

            # Accumulate all tool results across iterations for duplicate detection fallback
            all_tool_results = []

            while tool_call_count < max_tool_calls:
                # Replay durable receipts before asking Gemini to propose the
                # next action.  This is the crash/reconnect path: a receipt is
                # authoritative and must advance the server cursor without a
                # second side effect.
                replay_parts = []
                approved_directive = (
                    None
                    if suppress_automatic_context
                    else self._approved_action_directive()
                )
                while approved_directive is not None:
                    try:
                        existing_receipt = self._approved_action_receipt(
                            approved_directive
                        )
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_receipt_unavailable",
                            detail=str(exc),
                        )
                        raise AssertionError("unreachable")
                    if existing_receipt is None:
                        break
                    try:
                        if not isinstance(existing_receipt, Mapping):
                            raise ValueError("receipt must be an object")
                        receipt_call_id = str(
                            existing_receipt.get("tool_call_id") or ""
                        ).strip()
                        receipt_tool_name = str(
                            existing_receipt.get("tool_name") or ""
                        ).strip()
                        if receipt_call_id != approved_directive.call_id:
                            raise ValueError("receipt tool_call_id mismatch")
                        if receipt_tool_name != approved_directive.tool:
                            raise ValueError("receipt tool_name mismatch")
                        if existing_receipt.get("success") is not True:
                            raise ValueError("receipt success is not true")
                        receipt_arguments_value = existing_receipt.get("arguments")
                        if receipt_arguments_value is None:
                            receipt_arguments_value = {}
                        _plain_json_object(receipt_arguments_value)
                        receipt_output_value = existing_receipt.get("result")
                        if not isinstance(receipt_output_value, str):
                            raise ValueError("receipt result must be a string")
                        receipt_output = receipt_output_value
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_receipt_mismatch",
                            detail=str(exc),
                        )
                        raise AssertionError("unreachable")
                    protected_receipt_arguments, protected_receipt_output = (
                        self._protect_approved_action_replay(
                            approved_directive,
                            receipt_output,
                        )
                    )
                    response_part = self._function_response_part(
                        approved_directive.tool,
                        protected_receipt_output,
                    )
                    # Gemini history must retain durable receipts as strict
                    # model FunctionCall / user FunctionResponse pairs.  The
                    # final user response is held for ``latest_message`` below
                    # because ChatSession appends that wire message itself.
                    history = getattr(chat, "history", None)
                    if not isinstance(history, list):
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_receipt_mismatch",
                            detail="Gemini chat history is unavailable",
                        )
                        raise AssertionError("unreachable")
                    try:
                        history.append(
                            genai.protos.Content(
                                role="model",
                                parts=[
                                    self._synthetic_function_call_part(
                                        approved_directive.tool,
                                        protected_receipt_arguments,
                                    )
                                ],
                            )
                        )
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_receipt_mismatch",
                            detail=f"Gemini receipt replay history append failed: {exc}",
                        )
                        raise AssertionError("unreachable")
                    replay_parts.append(response_part)
                    all_tool_results.append(protected_receipt_output)
                    self._accept_approved_action_receipt(
                        approved_directive,
                        existing_receipt,
                    )
                    next_directive = (
                        None
                        if suppress_automatic_context
                        else self._approved_action_directive()
                    )
                    if next_directive is not None:
                        # Keep every completed receipt except the final one as
                        # a fully materialized model/user pair.  The final
                        # FunctionResponse is sent as ``latest_message`` below
                        # so ChatSession adds it exactly once to the wire
                        # sequence (avoiding a user/user duplicate).
                        history.append(
                            genai.protos.Content(
                                role="user",
                                parts=[response_part],
                            )
                        )
                    approved_directive = next_directive
                if replay_parts:
                    latest_message = [replay_parts[-1]]
                planning_state = (
                    None
                    if suppress_automatic_context
                    else self._current_planning_state()
                )
                if (
                    approved_round_budget is None
                    and planning_state is not None
                    and getattr(planning_state, "plan", None)
                    and str(getattr(planning_state, "phase", ""))
                    in {"executing", "PlanningRunPhase.EXECUTING"}
                ):
                    try:
                        approved_round_budget = self._approved_action_budget(
                            planning_state,
                            tool_call_count,
                        )
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_receipt_mismatch",
                            detail=f"approved plan budget unavailable: {exc}",
                        )
                        raise AssertionError("unreachable")
                if approved_round_budget is not None:
                    max_tool_calls = approved_round_budget
                completed_final_round = bool(
                    planning_state is not None
                    and str(getattr(planning_state, "phase", ""))
                    in {"completed", "PlanningRunPhase.COMPLETED"}
                )
                if approved_directive is not None:
                    # Approved execution is a phase boundary.  The initial
                    # exposure snapshot may have been built while the run
                    # was still planning (and therefore intentionally hides
                    # mutation declarations).  Re-resolve the provider
                    # exposure for each server-owned action, then narrow that
                    # fresh snapshot to the one exact directive declaration.
                    # Never consult the raw registry or auto-load a pack here;
                    # the provider-neutral exposure layer remains the sole
                    # capability/ACL authority.
                    try:
                        refreshed_effective_tools = self._get_effective_gemini_tools(
                            story_context
                        )
                        round_effective_tools = self._filter_gemini_tools(
                            refreshed_effective_tools,
                            approved_directive.tool,
                        )
                        declaration_count = sum(
                            len(
                                list(
                                    getattr(tool, "function_declarations", [])
                                    or []
                                )
                            )
                            for tool in round_effective_tools
                        )
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_tool_unavailable",
                            detail=f"approved action tool exposure failed: {exc}",
                        )
                        raise AssertionError("unreachable")
                    if declaration_count != 1:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_tool_unavailable",
                            detail=(
                                "approved action requires exactly one exposed "
                                f"declaration; found {declaration_count}"
                            ),
                        )
                        raise AssertionError("unreachable")
                    round_tool_config = self._gemini_tool_config(
                        mode="ANY",
                        allowed_function_names=[approved_directive.tool],
                    )
                elif completed_final_round:
                    # ``tools=[]`` and ``NONE`` are explicit: model defaults
                    # must not re-open mutation capability after completion.
                    round_effective_tools = []
                    round_tool_config = self._gemini_tool_config(mode="NONE")
                else:
                    round_effective_tools = effective_tools
                    round_tool_config = None
                # Send the latest message
                try:
                    self._capture_context_request(
                        history=list(getattr(chat, "history", None) or context[:-1]),
                        latest_message=latest_message,
                        tools=round_effective_tools,
                        response_tokens=generation_config_kwargs["max_output_tokens"],
                        request_kind=(
                            "initial_generation"
                            if tool_call_count == 0
                            else "tool_follow_up"
                        ),
                        model_name=routed_model or self.model_name,
                    )
                    try:
                        response = self._send_message_with_deadline(
                            chat,
                            latest_message,
                            generation_config=generation_config,
                            tools=round_effective_tools,
                            tool_config=round_tool_config,
                            privacy_payload=image_data if tool_call_count == 0 else None,
                        )
                    except Exception as send_error:
                        if approved_directive is not None:
                            # A pending approved action has no safe provider
                            # fallback.  Transport failure must terminalize the
                            # planning run instead of returning a normal answer
                            # (or retrying with a potentially divergent
                            # request).
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_transport_failed",
                                detail=str(send_error),
                            )
                            raise AssertionError("unreachable")
                        if not uses_thought_parts:
                            raise
                        # include_thoughts を受け付けないサーバー向けに1度だけ外して再試行する。
                        print(
                            "[GeminiLLMClient] include_thoughts付きリクエストが失敗したため"
                            f"除外して再試行します: {send_error}"
                        )
                        generation_config = _gemini_generation_config_without_thoughts(
                            generation_config_kwargs
                        )
                        uses_thought_parts = False
                        response = self._send_message_with_deadline(
                            chat,
                            latest_message,
                            generation_config=generation_config,
                            tools=round_effective_tools,
                            tool_config=round_tool_config,
                            privacy_payload=image_data if tool_call_count == 0 else None,
                        )
                    self._reconcile_context_usage(response)
                    self._record_gemini_usage(
                        response,
                        model_name=routed_model or self.model_name,
                        request_type=(
                            "chat" if tool_call_count == 0 else "tool"
                        ),
                        is_streaming=bool(stream),
                    )
                except GenerationInterrupted:
                    raise
                except PlanningInteractionTerminated:
                    # ``fail_approved_action`` raises this control-flow
                    # signal.  Keep it outside the provider fallback handler
                    # so a pending action can never be reported as success.
                    raise
                except Exception as e:
                    print(f"[GeminiLLMClient] Gemini API呼び出しエラー: {e}")
                    if approved_directive is not None:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_transport_failed",
                            detail=str(e),
                        )
                        raise AssertionError("unreachable")
                    if suppress_automatic_context:
                        # A generic fallback is not Guide-grounded.  Surface
                        # the transport failure to the outer Help boundary so
                        # it can return the deterministic unavailable message.
                        raise
                    if self.config.get("free_team.propagate_errors", False):
                        try:
                            e.free_team_side_effect_started = tool_call_count > 0
                        except Exception:
                            pass
                        raise
                    # フォールバック応答
                    fallback = self._get_fallback_response()
                    if not suppress_automatic_context:
                        self.conversation_history.append(
                            {"role": "user", "content": user_input}
                        )
                        self.conversation_history.append(
                            {"role": "assistant", "content": fallback}
                        )

                    if stream:

                        def error_generator():
                            yield fallback

                        return error_generator()
                    return fallback

                # Check if the response contains function calls - safely check candidates
                try:
                    candidates = getattr(response, "candidates", [])
                    if not candidates or len(candidates) == 0:
                        print(
                            f"[GeminiLLMClient] 警告: レスポンスにcandidatesがありません"
                        )
                        if approved_directive is not None:
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_no_candidates",
                            )
                            raise AssertionError("unreachable")
                        break

                    candidate = candidates[0]
                    if not hasattr(candidate, "content") or not candidate.content:
                        print(f"[GeminiLLMClient] 警告: candidateにcontentがありません")
                        if approved_directive is not None:
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_no_content",
                            )
                            raise AssertionError("unreachable")
                        break

                    if not hasattr(candidate.content, "parts"):
                        print(f"[GeminiLLMClient] 警告: contentにpartsがありません")
                        if approved_directive is not None:
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_no_parts",
                            )
                            raise AssertionError("unreachable")
                        break

                    parts = candidate.content.parts
                    if parts is None:
                        print(f"[GeminiLLMClient] 警告: contentにpartsがありません")
                        if approved_directive is not None:
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_no_parts",
                            )
                            raise AssertionError("unreachable")
                        break
                    if not parts:
                        print(f"[GeminiLLMClient] 警告: content partsが空です")
                        if approved_directive is not None:
                            self._fail_approved_action(
                                approved_directive,
                                reason="approved_action_empty_response",
                            )
                            raise AssertionError("unreachable")
                        break
                except PlanningInteractionTerminated:
                    raise
                except Exception as e:
                    print(f"[GeminiLLMClient] レスポンス解析エラー: {e}")
                    if approved_directive is not None:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_response_invalid",
                            detail=str(e),
                        )
                        raise AssertionError("unreachable")
                    break

                # Look for function calls
                function_calls = []
                text_parts = []
                thought_parts = []

                try:
                    if isinstance(parts, (str, bytes, bytearray)):
                        raise TypeError("Gemini response parts must be a sequence")
                    for part in parts:
                        if hasattr(part, "function_call") and part.function_call:
                            function_calls.append(part.function_call)
                            continue
                        part_text = getattr(part, "text", None)
                        if not part_text:
                            continue
                        # thought part を本文へ混ぜない（include_thoughts有効時のみ発生）。
                        if _gemini_part_is_thought(part):
                            thought_parts.append(part_text)
                            continue
                        text_parts.append(part_text)
                except PlanningInteractionTerminated:
                    raise
                except Exception as e:
                    print(f"[GeminiLLMClient] レスポンスparts解析エラー: {e}")
                    if approved_directive is not None:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_response_invalid",
                            detail=str(e),
                        )
                        raise AssertionError("unreachable")

                # A completed plan has no remaining approved action.  Even if
                # Gemini violates the explicit NONE/tool-less request and
                # returns a stale or malicious function call, terminalize the
                # plan before any ordinary tool/router or human-interaction
                # path can observe it.
                if completed_final_round and function_calls:
                    self._fail_completed_plan_provider_call(function_calls)

                if approved_directive is not None and not function_calls and not text_parts:
                    self._fail_approved_action(
                        approved_directive,
                        reason="approved_action_empty_response",
                    )
                    raise AssertionError("unreachable")

                # 中間・最終どちらのラウンドでも思考はそのまま配信する。
                for thought_text in thought_parts:
                    emit_thinking(
                        turn_event_emitter,
                        thought_text,
                        round_index=tool_call_count,
                    )

                if approved_directive is not None:
                    # The server-owned cursor requires exactly one function
                    # call from the one declaration exposed for this round.
                    # Reject malformed/plain/multi-call responses before any
                    # mutation can reach the router.
                    if not function_calls:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_plain_final_rejected",
                        )
                        raise AssertionError("unreachable")
                    if len(function_calls) != 1:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_multiple_calls_rejected",
                        )
                        raise AssertionError("unreachable")
                    try:
                        candidate_name = str(
                            getattr(function_calls[0], "name", "") or ""
                        ).strip()
                    except Exception as exc:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_response_invalid",
                            detail=str(exc),
                        )
                        raise AssertionError("unreachable")
                    if candidate_name != approved_directive.tool:
                        self._fail_approved_action(
                            approved_directive,
                            reason="approved_action_wrong_tool",
                            detail=candidate_name,
                        )
                        raise AssertionError("unreachable")

                if function_calls:
                    # ツール呼び出しを伴うラウンドの通常テキストは途中経過として配信する。
                    # 最終回答は既存の最終出力経路に任せ、ここでは発行しない。
                    emit_assistant_text(
                        turn_event_emitter,
                        "".join(text_parts),
                        round_index=tool_call_count,
                    )
                    # Execute function calls
                    tool_call_count += 1
                    function_results = []
                    results_text = []

                    # Initialize generated images list for this turn
                    generated_image_tags = []

                    # Track tool calls to detect duplicates
                    current_calls = []
                    duplicate_detected = False

                    for function_call_index, func_call in enumerate(function_calls):
                        if approved_directive is not None:
                            try:
                                function_name = str(
                                    getattr(func_call, "name", "") or ""
                                ).strip()
                                proposed_arguments = _plain_json_object(
                                    getattr(func_call, "args", None) or {}
                                )
                                proposed_arguments = _plain_json_object(
                                    self._privacy_gateway.restore_tool_arguments(
                                        proposed_arguments,
                                        tool_name=function_name,
                                    )
                                )
                            except Exception as exc:
                                self._fail_approved_action(
                                    approved_directive,
                                    reason="approved_action_arguments_invalid",
                                    detail=str(exc),
                                )
                                raise AssertionError("unreachable")
                            try:
                                arguments = self._approved_action_call(
                                    approved_directive,
                                    tool_name=function_name,
                                    arguments=proposed_arguments,
                                )
                            except PlanningInteractionTerminated:
                                raise
                            except Exception as exc:
                                self._fail_approved_action(
                                    approved_directive,
                                    reason="approved_action_arguments_invalid",
                                    detail=str(exc),
                                )
                                raise AssertionError("unreachable")
                            operation_id = approved_directive.call_id
                            emit_tool_start(
                                turn_event_emitter,
                                tool=function_name,
                                arguments=arguments,
                                operation_id=operation_id,
                            )
                            try:
                                unified_result = self._execute_tool_result(
                                    function_name,
                                    arguments,
                                    call_id=operation_id,
                                )
                            except PlanningInteractionTerminated:
                                raise
                            except Exception as exc:
                                self._fail_approved_action(
                                    approved_directive,
                                    reason="approved_action_tool_failed",
                                    detail=str(exc),
                                )
                                raise AssertionError("unreachable")
                            result_text = str(unified_result.model_output)
                            try:
                                protected_result = self._privacy_gateway.protect_sync(
                                    result_text,
                                    provider="gemini",
                                    source_kind="tool_result",
                                )
                                protected_payload = protected_result.payload
                                if not isinstance(protected_payload, str):
                                    raise ValueError(
                                        "protected approved tool result must be a string"
                                    )
                                result_text = protected_payload
                            except Exception as exc:
                                self._fail_approved_action(
                                    approved_directive,
                                    reason="approved_action_privacy_protection_failed",
                                    detail=str(exc),
                                )
                                raise AssertionError("unreachable")
                            emit_tool_end(
                                turn_event_emitter,
                                tool=function_name,
                                arguments=arguments,
                                output=result_text,
                                error=(
                                    unified_result.error
                                    if not unified_result.success
                                    else ""
                                ),
                                operation_id=operation_id,
                            )
                            results_text.append(result_text)
                            all_tool_results.append(result_text)
                            function_results.append(
                                {
                                    "function_response": {
                                        "name": function_name,
                                        "response": {"result": result_text},
                                    }
                                }
                            )
                            # Persist the receipt and advance the server cursor
                            # only after the tool result is known.  The helper
                            # reuses an existing receipt if a crash/replay
                            # raced this execution.
                            try:
                                self._persist_approved_action_result(
                                    approved_directive,
                                    arguments=arguments,
                                    result=result_text,
                                    success=bool(unified_result.success),
                                )
                            except PlanningInteractionTerminated:
                                raise
                            except Exception as exc:
                                self._fail_approved_action(
                                    approved_directive,
                                    reason="approved_action_receipt_unavailable",
                                    detail=str(exc),
                                )
                                raise AssertionError("unreachable")
                            continue

                        function_name = func_call.name
                        try:
                            arguments = _plain_json_object(
                                getattr(func_call, "args", None) or {}
                            )
                        except (TypeError, ValueError) as exc:
                            # Ordinary Gemini calls cannot safely execute
                            # provider objects either; fail this round without
                            # stringifying nested proto values.
                            raise ValueError(
                                f"invalid Gemini function arguments for {function_name}: {exc}"
                            ) from exc
                        arguments = self._privacy_gateway.restore_tool_arguments(
                            arguments,
                            tool_name=function_name,
                        )

                        # Create signature for duplicate detection
                        call_signature = (
                            function_name,
                            json.dumps(
                                arguments,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )

                        # Check for duplicate calls within this session
                        if not hasattr(self, "_recent_tool_calls"):
                            self._recent_tool_calls = []

                        if call_signature in self._recent_tool_calls:
                            print(
                                f"[GeminiLLMClient] 重複ツール呼び出しを検出: {function_name} - スキップしてLLMに指示を送ります"
                            )

                            # Instead of breaking, send a system instruction back to the model
                            # This forces the model to use the previous results
                            result = "システム通知: このツールは既に実行済みで、結果は取得されています。これ以上同じ検索を行わず、直前のステップで得られた検索結果「のみ」を使用して、ユーザーの質問に回答してください。"
                            results_text.append(result)

                            function_results.append(
                                {
                                    "function_response": {
                                        "name": function_name,
                                        "response": {"result": result},
                                    }
                                }
                            )
                            continue

                        # Add to recent calls
                        self._recent_tool_calls.append(call_signature)
                        current_calls.append(call_signature)

                        # Execute the tool
                        operation_id = str(uuid.uuid4())
                        emit_tool_start(
                            turn_event_emitter,
                            tool=function_name,
                            arguments=arguments,
                            operation_id=operation_id,
                        )
                        try:
                            result = self._execute_tool(function_name, arguments)
                        except Exception as tool_error:
                            emit_tool_end(
                                turn_event_emitter,
                                tool=function_name,
                                arguments=arguments,
                                output="",
                                error=str(tool_error),
                                operation_id=operation_id,
                            )
                            raise
                        emit_tool_end(
                            turn_event_emitter,
                            tool=function_name,
                            arguments=arguments,
                            output=str(result),
                            operation_id=operation_id,
                        )
                        protected_result = self._privacy_gateway.protect_sync(
                            result,
                            provider="gemini",
                            source_kind="tool_result",
                        )
                        result = str(protected_result.payload)
                        results_text.append(result)
                        all_tool_results.append(result)  # Accumulate across iterations

                        # Track generated images content
                        if function_name == "generate_image":
                            # result is [GENERATED_IMAGE:path]
                            generated_image_tags.append(result)

                        # Prepare function response
                        function_results.append(
                            {
                                "function_response": {
                                    "name": function_name,
                                    "response": {"result": result},
                                }
                            }
                        )

                        # A provider batch is only a proposal.  If the first
                        # call obtains approval, defer every later call in that
                        # same batch until the next server-controlled round;
                        # never let a stale provider batch perform mutations.
                        if (
                            function_name == "submit_plan_for_approval"
                            and (
                                state_after_approval := (
                                    None
                                    if suppress_automatic_context
                                    else self._current_planning_state()
                                )
                            )
                            is not None
                            and str(
                                getattr(state_after_approval, "phase", "")
                            )
                            in {"executing", "PlanningRunPhase.EXECUTING"}
                        ):
                            for skipped in function_calls[function_call_index + 1 :]:
                                skipped_name = str(
                                    getattr(skipped, "name", "") or ""
                                ).strip()
                                skipped_result = (
                                    "Not executed: approved actions start on the next "
                                    "server-controlled round."
                                )
                                function_results.append(
                                    {
                                        "function_response": {
                                            "name": skipped_name,
                                            "response": {"result": skipped_result},
                                        }
                                    }
                                )
                            break

                    # Approval may have been granted in the last legacy-cap
                    # round (for example, round five).  Extend the loop before
                    # its ``while`` condition is evaluated so the server-owned
                    # actions and mandatory final round still run.
                    if approved_round_budget is None:
                        state_after_batch = (
                            None
                            if suppress_automatic_context
                            else self._current_planning_state()
                        )
                        if (
                            state_after_batch is not None
                            and getattr(state_after_batch, "plan", None)
                            and str(getattr(state_after_batch, "phase", ""))
                            in {"executing", "PlanningRunPhase.EXECUTING"}
                        ):
                            try:
                                approved_round_budget = self._approved_action_budget(
                                    state_after_batch,
                                    tool_call_count,
                                )
                            except Exception as exc:
                                self._fail_approved_action(
                                    self._approved_action_directive(),
                                    reason="approved_action_receipt_mismatch",
                                    detail=f"approved plan budget unavailable: {exc}",
                                )
                                raise AssertionError("unreachable")
                            max_tool_calls = approved_round_budget

                    # Clear recent calls after successful non-duplicate execution
                    if tool_call_count >= max_tool_calls:
                        self._recent_tool_calls = []

                    # For queue operations, return immediately.
                    if (
                        len(function_calls) == 1
                        and user_input
                        and ("キューに" in user_input or "追加" in user_input)
                    ):
                        # ... (existing queue logic)
                        self._recent_tool_calls = []
                        return results_text[0]

                    # Send function results back to the model for multiple or complex calls
                    if function_results:
                        # Build proper FunctionResponse parts for Gemini API
                        # Using genai.protos.Part with FunctionResponse for correct format
                        function_response_parts = []
                        for fr in function_results:
                            func_resp = fr.get("function_response", {})
                            func_name = func_resp.get("name", "")
                            func_result = func_resp.get("response", {})
                            # Create FunctionResponse part using protos
                            function_response_parts.append(
                                genai.protos.Part(
                                    function_response=genai.protos.FunctionResponse(
                                        name=func_name, response=func_result
                                    )
                                )
                            )

                        # Update latest_message to send function results back to the model
                        # This is critical - without this, the loop would re-send the original user message
                        latest_message = function_response_parts
                        continue

                # If we have text response, return it
                if text_parts:
                    response_text = "".join(text_parts)

                    # Sanitize hallucinated placeholders
                    import re

                    response_text = re.sub(
                        r"\{get_generated_image_html\(.*?\)\}", "", response_text
                    ).strip()

                    # Append any generated image tags to the final response
                    if "generated_image_tags" in locals() and generated_image_tags:
                        response_text += "\n" + "\n".join(generated_image_tags)

                    if not completed_final_round and not suppress_automatic_context:
                        response_text = self._run_agentic_completion_from_valid_response(
                            context=self._render_gemini_context_for_review(
                                context,
                                latest_message,
                            ),
                            user_input=user_input,
                            initial_response=response_text,
                            generation_config=generation_config,
                            is_streaming=bool(stream),
                        )
                    response_text = str(
                        self._privacy_gateway.restore_aliases(response_text)
                    )
                    if not suppress_automatic_context:
                        response_text = self._finalize_roleplay_response_sync(
                            response_text,
                            stream_callback=stream_callback,
                        )

                    # Add to history (under lock to prevent interleaving with concurrent requests)
                    if not suppress_automatic_context:
                        with self._history_lock:
                            self.conversation_history.append(
                                {"role": "user", "content": user_input}
                            )
                            self.conversation_history.append(
                                {"role": "assistant", "content": response_text}
                            )

                    # Save assistant response to memory
                    if (
                        not suppress_automatic_context
                        and self.memory_manager
                        and self._memory_enabled
                        and not external_persistence
                    ):
                        try:
                            if session_id:
                                # Manifest generation must observe this completed turn,
                                # not whichever turn the memory loop executes under later.
                                assistant_metadata = self._get_memory_metadata()
                                self._safe_memory_operation(
                                    self._save_assistant_message_to_session,
                                    response_text,
                                    session_id,
                                    assistant_metadata,
                                    fire_and_forget=True,
                                )
                            # Note: Skip saving if no session_id to avoid project_id=None sessions
                        except Exception as e:
                            print(
                                f"[GeminiLLMClient] Failed to save assistant message to memory: {e}"
                            )

                    # Semantic memory processing now handled by ResponseHandler

                    print(
                        f"[GeminiLLMClient] 応答生成 (ツール呼び出し{tool_call_count}回): {len(response_text)}文字"
                    )

                    if stream:

                        def response_generator():
                            yield response_text

                        return response_generator()
                    return response_text

                # If no function calls and no text, break
                break

            # If we exhausted max_tool_calls but have tool results, try to get a final response
            if (
                tool_call_count >= max_tool_calls
                and all_tool_results
                and not completed_final_round
            ):
                print(
                    f"[GeminiLLMClient] ツール呼び出し上限({max_tool_calls})に達しました。最終応答を生成します..."
                )
                try:
                    # Send a prompt asking for final response based on all the tool results
                    final_prompt = "上記のツール実行結果を使って、ユーザーの質問に対する回答を生成してください。"
                    self._capture_context_request(
                        history=list(getattr(chat, "history", None) or context[:-1]),
                        latest_message=final_prompt,
                        tools=effective_tools,
                        response_tokens=generation_config_kwargs["max_output_tokens"],
                        request_kind="tool_limit_finalization",
                        model_name=routed_model or self.model_name,
                    )
                    final_response = self._send_message_with_deadline(
                        chat,
                        final_prompt, generation_config=generation_config
                    )
                    self._reconcile_context_usage(final_response)
                    self._record_gemini_usage(
                        final_response,
                        model_name=routed_model or self.model_name,
                        request_type="tool",
                        is_streaming=bool(stream),
                    )

                    if (
                        final_response.candidates
                        and final_response.candidates[0].content.parts
                    ):
                        for part in final_response.candidates[0].content.parts:
                            if _gemini_part_is_thought(part):
                                emit_thinking(
                                    turn_event_emitter,
                                    getattr(part, "text", ""),
                                    round_index=tool_call_count,
                                )
                                continue
                            if hasattr(part, "text") and part.text:
                                response_text = part.text
                                if not suppress_automatic_context:
                                    response_text = (
                                        self._run_agentic_completion_from_valid_response(
                                            context=(
                                                self._render_gemini_context_for_review(
                                                    context,
                                                    latest_message,
                                                )
                                            ),
                                            user_input=user_input,
                                            initial_response=response_text,
                                            generation_config=generation_config,
                                            is_streaming=bool(stream),
                                        )
                                    )
                                response_text = str(
                                    self._privacy_gateway.restore_aliases(response_text)
                                )
                                if not suppress_automatic_context:
                                    response_text = self._finalize_roleplay_response_sync(
                                        response_text,
                                        stream_callback=stream_callback,
                                    )
                                if not suppress_automatic_context:
                                    with self._history_lock:
                                        self.conversation_history.append(
                                            {"role": "user", "content": user_input}
                                        )
                                        self.conversation_history.append(
                                            {"role": "assistant", "content": response_text}
                                        )
                                print(
                                    f"[GeminiLLMClient] 最終応答生成: {len(response_text)}文字"
                                )

                                if stream:

                                    def response_generator():
                                        yield response_text

                                    return response_generator()
                                return response_text
                except Exception as e:
                    print(f"[GeminiLLMClient] 最終応答生成エラー: {e}")
                    if self.config.get("free_team.propagate_errors", False):
                        raise

            # Fallback if no valid response
            if suppress_automatic_context:
                raise RuntimeError("Gemini returned no valid Help response")
            if self.config.get("free_team.propagate_errors", False):
                raise RuntimeError("Gemini returned no valid response")
            fallback = self._get_fallback_response()
            if not suppress_automatic_context:
                with self._history_lock:
                    self.conversation_history.append(
                        {"role": "user", "content": user_input}
                    )
                    self.conversation_history.append(
                        {"role": "assistant", "content": fallback}
                    )

            if stream:

                def fallback_generator():
                    yield fallback

                return fallback_generator()
            return fallback

        except PlanningInteractionTerminated:
            # Keep planning control-flow visible to the terminal response
            # handler instead of converting it into a normal fallback answer.
            raise
        except Exception as e:
            print(f"[GeminiLLMClient] エラー: {e}")
            import traceback

            traceback.print_exc()
            if suppress_automatic_context:
                # Do not fabricate a character fallback for a Guide-only turn.
                raise
            if self.config.get("free_team.propagate_errors", False):
                raise

            fallback = self._get_fallback_response()

            # Add to history even on error, except for isolated controller
            # turns whose provider context must never leak into normal chat.
            if not suppress_automatic_context:
                with self._history_lock:
                    self.conversation_history.append(
                        {"role": "user", "content": user_input}
                    )
                    self.conversation_history.append(
                        {"role": "assistant", "content": fallback}
                    )

            if stream:

                def error_generator():
                    yield fallback

                return error_generator()
            return fallback
        finally:
            if suppress_automatic_context and isolated_history_snapshot is not None:
                saved_history, saved_loaded_session_id = isolated_history_snapshot
                with self._history_lock:
                    try:
                        self.conversation_history = copy.deepcopy(saved_history)
                    except Exception:
                        self.conversation_history = list(saved_history or [])
                    self._loaded_session_id = saved_loaded_session_id
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            capture_context_manifest_before_context_clear(self)
            self._current_context_bundle = None
            if isolated_privacy_gateway_active:
                self._privacy_gateway = isolated_privacy_gateway_previous

    def _get_fallback_response(self) -> str:
        """Get fallback response for errors"""
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            personality = character_config.get("personality", {})
            return personality.get(
                "fallbackReply",
                "すみません、うまく聞き取れませんでした。もう一度お話しください。",
            )
        return "すみません、うまく聞き取れませんでした。もう一度お話しください。"

    def _finalize_roleplay_response_sync(
        self,
        response_text: str,
        *,
        stream_callback: Any = None,
    ) -> str:
        """Gemini 応答に SCENE_DESCRIPTION があれば画像生成し、表示用テキストへ整形する。"""
        from ..services.roleplay_scene_image import finalize_roleplay_assistant_response

        existing = getattr(self, "current_assistant_message_id", None)
        if not existing:
            self.current_assistant_message_id = str(uuid.uuid4())
        assistant_message_id = self.current_assistant_message_id

        async def _run() -> str:
            return await finalize_roleplay_assistant_response(
                self,
                response_text,
                message_id=assistant_message_id,
                history=list(self.conversation_history),
                stream_callback=stream_callback,
            )

        try:
            result = self._safe_memory_operation(_run, timeout=120)
        except Exception:
            print("[GeminiLLMClient] Roleplay 画像生成に失敗したため本文のみ返します")
            return response_text
        if isinstance(result, str) and result.strip():
            return result
        return response_text

    def _run_memory_loop(self):
        """Run the persistent memory event loop in a background thread"""
        asyncio.set_event_loop(self._memory_loop)
        self._memory_loop.run_forever()

    async def _warmup_cross_session_memory(self):
        """Pre-initialize cross-session memory (embedding model + Qdrant) at startup"""
        try:
            from ..memory.cross_session_memory import get_cross_session_memory

            csm = get_cross_session_memory()
            initialized = await csm.initialize()
            if initialized:
                logger.info(
                    "Gemini cross-session memory pre-initialized",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            else:
                logger.warning(
                    "Gemini cross-session semantic memory unavailable; continuing without it",
                )
        except Exception:
            logger.warning(
                "Gemini cross-session semantic memory degraded; continuing without it",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )

    def _safe_memory_operation(
        self, operation_func, *args, timeout=30, fire_and_forget=False
    ):
        """Execute async memory operations on the persistent memory event loop

        Args:
            operation_func: Async function to execute
            *args: Arguments to pass to the function
            timeout: Timeout in seconds for blocking calls
            fire_and_forget: If True, submit and return immediately without waiting
        """
        if not self._memory_loop or not self._memory_loop.is_running():
            print("[GeminiLLMClient] Memory loop not available")
            return None

        future = asyncio.run_coroutine_threadsafe(
            operation_func(*args), self._memory_loop
        )

        if fire_and_forget:

            def _on_done(f):
                try:
                    exc = f.exception()
                except asyncio.CancelledError:
                    return
                if exc:
                    print(f"[GeminiLLMClient] Background memory op failed: {exc}")

            future.add_done_callback(_on_done)
            return None

        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            print(f"[GeminiLLMClient] Memory operation timed out")
            return None
        except Exception as e:
            print(f"[GeminiLLMClient] Memory operation failed: {e}")
            return None

    async def _save_user_message_to_memory(self, user_input: str):
        """Save user message to memory asynchronously

        Args:
            user_input: User input text
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        await self.memory_manager.add_message(
            user_id=self._get_session_user_id(),
            character_name=self.character_name,
            role="user",
            content=user_input,
            metadata=self._get_memory_metadata(),
            llm_client=self,
        )

    async def _save_assistant_message_to_memory(self, response_text: str):
        """Save assistant message to memory asynchronously

        Args:
            response_text: Assistant response text
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        await self.memory_manager.add_message(
            user_id=self._get_session_user_id(),
            character_name=self.character_name,
            role="assistant",
            content=response_text,
            metadata=self._get_memory_metadata(),
            llm_client=self,
        )

    async def _load_session_history(self, session_id: str) -> List[Dict[str, str]]:
        """Load conversation history from a specific session

        Args:
            session_id: Session ID to load history from

        Returns:
            List of message dicts with role and content
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        try:
            from ..services.privacy_masking_projection import (
                is_privacy_masking_source,
            )

            messages = await self.memory_manager.repository.get_session_messages(
                session_id
            )

            # Convert to conversation_history format
            history = []
            for msg in messages:
                # The raw user row for a masking turn is retained for audit/UI,
                # but it is never valid provider prompt context.  Keep this
                # direct Gemini history loader aligned with the shared memory
                # projection used by other LLM clients.
                if is_privacy_masking_source(msg):
                    continue
                history.append(
                    {
                        "role": "user" if msg.role == "user" else "assistant",
                        "content": msg.content,
                    }
                )

            return history
        except Exception as e:
            print(f"[GeminiLLMClient] Failed to load session history: {e}")
            return []

    async def _save_user_message_to_session(
        self,
        user_input: str,
        session_id: str,
        branch_from_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Save user message to specific session

        Args:
            user_input: User input text
            session_id: Session ID to save to
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        persisted_metadata = (
            dict(metadata)
            if metadata is not None
            else self._get_user_memory_metadata()
        )
        persisted_metadata.pop("context_manifest", None)
        await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="user",
            content=user_input,
            metadata=persisted_metadata,
            branch_from_message_id=branch_from_message_id,
        )

    async def _save_assistant_message_to_session(
        self,
        response_text: str,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Save assistant message to specific session

        Args:
            response_text: Assistant response text
            session_id: Session ID to save to
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        persisted_metadata = (
            dict(metadata)
            if metadata is not None
            else self._get_memory_metadata()
        )
        await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="assistant",
            content=response_text,
            metadata=persisted_metadata,
            message_id=getattr(self, "current_assistant_message_id", None),
        )

    def _plain_text_max_output_tokens(self) -> int:
        default = 4096
        if not self.config:
            return default
        configured = self.config.get("llm.gemini.plain_text_max_output_tokens")
        if configured is None:
            return default
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            return default

    def _generate_plain_text(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        request_type: str = "plain",
    ) -> str:
        """Generate ephemeral text while preserving a reused Help client."""

        suppress_automatic_context = bool(
            getattr(get_turn_context(), "suppress_automatic_context", False)
        )
        if not suppress_automatic_context:
            return self._generate_plain_text_impl(
                prompt,
                system_prompt=system_prompt,
                request_type=request_type,
            )

        previous_gateway = getattr(self, "_privacy_gateway", None)
        previous_session_context = getattr(self, "_privacy_session_context", None)
        previous_project_metadata = getattr(self, "_privacy_project_metadata", None)
        try:
            return self._generate_plain_text_impl(
                prompt,
                # A Help provider helper must not accept a stale/custom system
                # instruction as an alternate authority.
                system_prompt=AOITALK_HELP_ISOLATED_SYSTEM_PROMPT,
                request_type=request_type,
            )
        finally:
            self._privacy_gateway = previous_gateway
            self._privacy_session_context = previous_session_context
            self._privacy_project_metadata = previous_project_metadata

    def _generate_plain_text_impl(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        request_type: str = "plain",
    ) -> str:
        """Generate text with a fresh tool-free model and no conversation state."""
        self._refresh_plain_text_privacy_gateway()
        model_kwargs: Dict[str, Any] = {
            "model_name": self.model_name,
            "safety_settings": self._safety_settings,
        }
        if system_prompt:
            model_kwargs["system_instruction"] = system_prompt
        model = genai.GenerativeModel(**model_kwargs)
        generation_config, _ = _gemini_generation_config(
            {
                "temperature": 0.2,
                "max_output_tokens": self._plain_text_max_output_tokens(),
                "candidate_count": 1,
            }
        )
        descriptor = EgressDescriptor(
            action="model.generate",
            transport="gemini.generate_content",
            destination="https://generativelanguage.googleapis.com",
            provider="gemini",
            model=str(self.model_name or ""),
        )

        def send_plain_text_request(outbound: dict[str, Any]) -> Any:
            if not isinstance(outbound, dict) or "prompt" not in outbound:
                raise PrivacyError("Gemini plain-text prompt is missing")
            outbound_prompt = outbound.get("prompt")
            if not isinstance(outbound_prompt, str) or not outbound_prompt:
                raise PrivacyError("Gemini plain-text prompt is empty")
            if "generation_config" not in outbound:
                raise PrivacyError("Gemini generation config is missing")
            return model.generate_content(
                outbound_prompt,
                generation_config=outbound.get("generation_config"),
            )

        response = self._privacy_gateway.execute_sync(
            {
                "prompt": prompt or "",
                "generation_config": generation_config,
            },
            provider="gemini",
            descriptor=descriptor,
            sender=send_plain_text_request,
            source_kind=request_type,
            model=str(self.model_name or ""),
        )
        self._record_gemini_usage(
            response,
            model_name=self.model_name,
            request_type=request_type,
        )
        return self._privacy_gateway.restore(
            str(getattr(response, "text", "") or "")
        )

    def _refresh_plain_text_privacy_gateway(self) -> OutboundPrivacyGateway:
        """Refresh request identity/policy before side-effect-free generation."""

        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
        except Exception:
            turn = None
        suppress_automatic_context = bool(
            getattr(turn, "suppress_automatic_context", False)
        )
        inherited = get_privacy_policy_context()
        user_id = str(self._get_session_user_id() or "")
        if not user_id or user_id == "default_user":
            user_id = str(getattr(turn, "user_id", None) or user_id)
        session_id = str(
            self.current_session_id
            or getattr(turn, "session_id", None)
            or ""
        )
        if suppress_automatic_context:
            # A reused Gemini client may still carry the previous session's
            # actor.  Help's TurnContext is authoritative for this isolated
            # request, so prefer it whenever it is present.
            user_id = str(
                getattr(turn, "user_id", None)
                or self._get_session_user_id()
                or ""
            )
            session_id = str(
                getattr(turn, "session_id", None)
                or self.current_session_id
                or ""
            )
            gateway = OutboundPrivacyGateway(
                self.config,
                user_id=user_id,
                session_id=session_id,
                session_context={},
                project_metadata={},
            )
            setattr(gateway, "_aoitalk_help_isolated", True)
            self._privacy_gateway = gateway
            return gateway
        session_context = (
            dict(inherited.session_context)
            if inherited.session_context is not None
            else (
                dict(self._privacy_session_context)
                if isinstance(self._privacy_session_context, dict)
                else {}
            )
        )
        project_metadata = (
            dict(inherited.project_metadata)
            if inherited.project_metadata is not None
            else (
                dict(self._privacy_project_metadata)
                if isinstance(self._privacy_project_metadata, dict)
                else {}
            )
        )
        self._privacy_session_context = session_context
        self._privacy_project_metadata = project_metadata
        gateway = getattr(self, "_privacy_gateway", None)
        if not isinstance(gateway, OutboundPrivacyGateway) or (
            gateway.user_id != user_id or gateway.session_id != session_id
        ):
            gateway = OutboundPrivacyGateway(
                self.config,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )
            self._privacy_gateway = gateway
        else:
            gateway.update_policy_context(
                session_context=session_context,
                project_metadata=project_metadata,
            )
        return gateway

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> str:
        """履歴・ツールを変更せずプレーンテキストを生成する。"""
        return await asyncio.to_thread(
            self._generate_plain_text,
            prompt,
            system_prompt=system_prompt,
            request_type="plain",
        )

    async def generate_title_async(self, prompt: str) -> str:
        """Generate a side-effect-free Gemini title and meter it separately."""

        return await asyncio.to_thread(
            self._generate_plain_text,
            prompt,
            request_type="title",
        )

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """履歴・ツールを変更せずGeminiでメモリ抽出を行う。"""

        return await asyncio.to_thread(
            self._generate_plain_text,
            prompt,
            system_prompt=system_prompt,
            request_type="memory_extraction",
        )

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
        """Async version of generate_response - Gemini API is synchronous, so this just wraps the sync call

        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            image_data: Optional image data for multimodal input
            stream_callback: Optional stream event callback
                (tool_start / tool_end / assistant_text / thinking)

        Returns:
            Generated response
        """
        # Gemini API is synchronous, so we just call the sync method
        import asyncio
        import contextvars
        import functools

        loop = asyncio.get_running_loop()
        # executor thread では実行中ループが見えないため、ここでループを束ねておく。
        bound_stream_callback = bind_stream_callback_loop(stream_callback)
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(
            None,
            ctx.run,
            functools.partial(
                self.generate_response,
                user_input,
                temperature,
                max_tokens,
                stream=False,
                image_data=image_data,
                stream_callback=bound_stream_callback,
            ),
        )

    def clear_history(self):
        """Clear conversation history - session creation is handled by frontend"""
        self.conversation_history = []
        if hasattr(self, "history_manager") and self.history_manager is not None:
            self.history_manager.clear()
        self._loaded_session_id = None  # Force DB reload on next request
        print(f"[GeminiLLMClient] 会話履歴をクリア")

        # Note: New session creation is handled by frontend (chat.js/conversation-history.js)
        # via API call to /api/conversations, so we don't create a new session here
        # to avoid creating duplicate sessions without project_id

    async def _start_new_memory_session(self):
        """Start a new memory session"""
        if self.memory_manager:
            await self.memory_manager.start_new_session(
                user_id=self._get_session_user_id(), character_name=self.character_name
            )

    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history

        Returns:
            List of conversation messages
        """
        return self.conversation_history.copy()

    async def cleanup(self):
        """Clean up resources including memory manager"""
        if self._cleanup_done:
            return

        self._cleanup_done = True
        cleanup_cancelled = False

        memory_loop = self._memory_loop
        memory_thread = self._memory_thread

        async def _await_threadsafe(future, *, timeout: float) -> bool:
            """Await a concurrent-futures result without closing its loop."""

            wrapped = asyncio.wrap_future(future)
            try:
                await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
            except asyncio.CancelledError:
                # Distinguish cancellation of the submitted future from
                # cancellation of this cleanup waiter.  The former is the
                # expected warmup shutdown result; the latter must be deferred
                # until all memory resources and the loop are released.
                if future.done():
                    try:
                        future.result()
                    except concurrent.futures.CancelledError:
                        pass
                    return False
                current = asyncio.current_task()
                if current is not None:
                    uncancel = getattr(current, "uncancel", None)
                    if callable(uncancel):
                        uncancel()
                return True
            except asyncio.TimeoutError:
                # Do not leave a submitted coroutine pending when the owner
                # is shutting its loop down.
                future.cancel()
                try:
                    await asyncio.wait_for(wrapped, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            return False

        async def _await_threadsafe_completion(future) -> None:
            """Await a cross-thread future to completion without detaching it.

            The ConversationMemoryManager owns its own cleanup task and uses a
            shield internally.  A cancellation of this caller must therefore
            be deferred until that future has completed, otherwise stopping
            the memory loop below could strand the manager's DB/index tasks.
            """

            wrapped = asyncio.wrap_future(future)
            caller_cancelled = False
            while not wrapped.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    if wrapped.done():
                        break
                    caller_cancelled = True
                    current = asyncio.current_task()
                    if current is not None:
                        uncancel = getattr(current, "uncancel", None)
                        if callable(uncancel):
                            uncancel()
                    continue
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                # The submitted future itself was cancelled; retrieve and
                # consume that expected outcome.
                pass
            if caller_cancelled:
                raise asyncio.CancelledError

        # The manager's owned indexing/summarization tasks run on the
        # persistent memory loop.  Cleanup must execute there and complete
        # before that loop is stopped; awaiting it from the caller's loop
        # afterwards would otherwise cross an already-closed event loop.
        if (
            memory_loop
            and memory_thread
            and memory_thread.is_alive()
            and not memory_loop.is_closed()
        ):
            warmup_future = self._memory_warmup_future
            if warmup_future is not None:
                if not warmup_future.done():
                    warmup_future.cancel()
                try:
                    # Await both a cancelled and an already-completed future
                    # so its result/exception is always retrieved.
                    cleanup_cancelled = (
                        await _await_threadsafe(warmup_future, timeout=5.0)
                        or cleanup_cancelled
                    )
                except Exception as e:
                    print(f"[GeminiLLMClient] Memory warmup cleanup error: {e}")

            if self.memory_manager:
                try:
                    cleanup_future = asyncio.run_coroutine_threadsafe(
                        self.memory_manager.cleanup(), memory_loop
                    )
                    # Do not use a timeout here.  ConversationMemoryManager
                    # may still be awaiting owned indexing/summarization tasks
                    # before closing its DB; stopping the loop at a timeout
                    # would strand those tasks and recreate shutdown warnings.
                    await _await_threadsafe_completion(cleanup_future)
                    print("[GeminiLLMClient] Memory manager cleaned up")
                except asyncio.CancelledError:
                    cleanup_cancelled = True
                except Exception as e:
                    print(f"[GeminiLLMClient] Error during memory cleanup: {e}")

            memory_loop.call_soon_threadsafe(memory_loop.stop)
            if memory_thread and memory_thread.is_alive():
                memory_thread.join(timeout=5)
            if (
                (memory_thread is None or not memory_thread.is_alive())
                and not memory_loop.is_closed()
            ):
                memory_loop.close()
            self._memory_warmup_future = None
        elif self.memory_manager:
            # Startup may fail before the persistent loop is running.  There
            # are no loop-owned background tasks in that case, so close on the
            # current loop as a fallback.
            try:
                await self.memory_manager.cleanup()
                print("[GeminiLLMClient] Memory manager cleaned up")
            except Exception as e:
                print(f"[GeminiLLMClient] Error during memory cleanup: {e}")

        print(f"[GeminiLLMClient] クリーンアップ完了")
        if cleanup_cancelled:
            raise asyncio.CancelledError


def create_gemini_client(config: Config) -> GeminiLLMClient:
    """Factory function to create Gemini LLM client

    Args:
        config: Application configuration

    Returns:
        Configured GeminiLLMClient instance
    """
    api_key = config.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Gemini API key not found in config or environment")

    model = config.get("llm_model", "gemini-3-flash-preview")

    return GeminiLLMClient(api_key=api_key, model=model, config=config)
