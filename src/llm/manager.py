"""LLM client manager for OpenAI, OpenRouter, Gemini, Ollama, SGLang, and CLI backends."""

import copy
import os
import logging
import re
import threading
from typing import Optional, List, Dict, Any, Union, Callable, Awaitable, TYPE_CHECKING

from ..config import Config
from ..services.project_context import (
    get_runtime_project_context,
)
from ..services.context_builder import ContextBundle
from .gemini_engine import GeminiLLMClient
from .native_runtime import (
    AgentDefinition as Agent,
    AgentTurnRunner,
    NativeModelSettings as ModelSettings,
    Reasoning,
    cloud_advisor_parent_scope,
    create_async_openai_client,
)
from .provider_capabilities import ProviderCapabilities
from ..tools import init_spotify_manager
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..reasoning import ReasoningManager
from .runtime_tool_registry import (
    build_runtime_tool_registry,
    build_runtime_tool_registry_for_client,
)
from .tool_packs import ensure_load_tool_pack_tool
from .generation_policy import DEFAULT_GENERATION_POLICY
from .planning_policy import DEFAULT_PLANNING_POLICY
from .conversation_context import conversation_state_mode
from .deployment_resolver import (
    KNOWN_PROVIDER_IDS,
    canonical_model_for_provider,
    effective_config_overrides,
    is_retired_model,
    preflight_deployment,
    resolve_llm_deployment,
)
from .manager_parts import (
    AgentSetupMixin,
    ContextBuildingMixin,
    GenerationApiMixin,
    MemoryIntegrationMixin,
    TurnExecutionMixin,
)
from ..utils.logging_config import FILE_ONLY_LOG_EXTRA

if TYPE_CHECKING:
    # 戻り値型注釈（文字列フォワードリファレンス）用。実行時importは循環回避のため行わない。
    from .cli_llm_client import CLILLMClient
    from .ollama_engine import OllamaClient

logger = logging.getLogger(__name__)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        try:
            value = config.get(key, None)
        except TypeError:
            value = config.get(key)
        if value is not None:
            return value
    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _config_bool(config: Any, key: str, default: bool) -> bool:
    raw_value = _config_get(config, key, None)
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]

_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_SEARCH_OUTPUT_LIMIT = 12000
_SEARCH_URL_LIMIT = 20
SteeringCallback = Callable[[], Awaitable[List[str]]]


class AgentLLMClient(
    AgentSetupMixin,
    ContextBuildingMixin,
    MemoryIntegrationMixin,
    TurnExecutionMixin,
    GenerationApiMixin,
):
    """Character-based LLM client using the AoiTalk-native agent runtime."""

    # Discord ingress must not inject user-scoped legacy history for this
    # client.  Native generation calls
    # ``_sync_history_with_current_session`` against the durable
    # ConversationSession before constructing each provider request.
    manages_conversation_session_history = True

    # The native runtime records the user turn before model execution.  When
    # ResponseHandler restarts after Ctrl+Enter, send only the new instruction
    # so the original user message is not persisted twice.
    steering_retry_uses_existing_history = True

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.6-luna",
        config: Optional[Config] = None,
        *,
        provider_label: str = "openai",
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
        force_chat_completions: bool = False,
        supports_tools: bool = True,
    ):
        """Initialize tool-based LLM client

        Args:
            api_key: OpenAI API key
            model: Model to use
            config: Application configuration (required)
        """
        self.config = config
        self._lightweight_ephemeral_client = _config_bool(
            config, "runtime.lightweight_ephemeral_client", False
        )
        normalized_model = str(model or "").strip()
        if is_retired_model(normalized_model):
            normalized_model = canonical_model_for_provider(
                config, provider_label, selected_model=None
            )
        if not normalized_model:
            normalized_model = canonical_model_for_provider(
                config, provider_label, selected_model=None
            )
        if not normalized_model or is_retired_model(normalized_model):
            raise ValueError(
                f"No safe model is configured for provider '{provider_label}'"
            )
        self.model_name = normalized_model
        self.provider_label = provider_label
        self._native_tools_enabled = (
            bool(supports_tools)
            and not self._lightweight_ephemeral_client
        )
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=self._native_tools_enabled,
            supports_response_format=provider_label in {"openai", "deepseek", "deepinfra"},
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=provider_label in {"openrouter", "deepseek", "deepinfra"},
        )
        self._openai_client = create_async_openai_client(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
        )
        self._turn_runner = AgentTurnRunner(
            client=self._openai_client,
            provider_label=provider_label,
            config=config,
        )
        # Handle both Config object and dict
        if self._lightweight_ephemeral_client:
            self.character_name = "Assistant"
        elif hasattr(config, "default_character"):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get("default_character", "Assistant")
        else:
            self.character_name = "Assistant"

        # Initialize HistoryManager
        from ..memory.history import HistoryManager

        self.history_manager = HistoryManager()
        self._summarization_task = None
        self._active_summarization_tasks = set()
        self._summarize_batch_size = 10
        self._summarize_threshold = 20

        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        # Loaded per-session/project privacy policy metadata.  The policy is
        # refreshed at the start of every turn and inherited by Agent Team
        # children through the privacy contextvar.
        self._privacy_session_context: Dict[str, Any] = {}
        self._privacy_project_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = (
            None  # For session-specific message storage
        )
        self.current_assistant_message_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.planning_policy = DEFAULT_PLANNING_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._last_context_snapshots: list[dict[str, Any]] = []
        self._loaded_history_session_id: Optional[str] = None
        self._model_transcript: list[dict[str, Any]] = []
        self._last_model_transcript: list[dict[str, Any]] = []
        self._history_authoritative_model_transcript: list[dict[str, Any]] = []
        self._history_active_model_transcript: list[dict[str, Any]] = []
        self._provider_state_mode = conversation_state_mode(config, provider_label)
        self._provider_state = {"previous_response_id": None, "fingerprint": None}
        # Native OpenAI turns publish a run-keyed completed ledger so
        # AgentRun fallback can still prove a committed mutation when the
        # final provider text request fails after the tool loop.
        self._native_completed_agent_run_states: dict[str, dict[str, Any]] = {}
        self._native_completed_agent_run_states_lock = threading.Lock()

        # Initialize memory manager
        self.memory_manager = None

        # Handle config access consistently
        def get_config_value(key, default=None):
            if hasattr(config, "get"):
                return config.get(key, default)
            elif isinstance(config, dict):
                return config.get(key, default)
            else:
                return default

        if self._lightweight_ephemeral_client:
            # Lightweight target clients are intentionally side-effect free:
            # no memory manager or cross-session persistence is allowed even
            # when a caller constructs one without the usual ephemeral flag.
            self._memory_enabled = False
        else:
            explicit_memory_enabled = get_config_value(
                "memory.enabled",
                None,
            )
            if explicit_memory_enabled is None:
                self._memory_enabled = get_config_value(
                    "memory",
                    {},
                ).get("enabled", True)
            else:
                self._memory_enabled = _config_bool(
                    config, "memory.enabled", True
                )
        if self._memory_enabled:
            memory_config = MemoryConfig()
            if config:
                memory_config.llm_provider = get_config_value(
                    "llm_provider", memory_config.llm_provider
                )
                memory_config.llm_model = get_config_value(
                    "llm_model", memory_config.llm_model
                )
                memory_settings = get_config_value("memory", {})
                memory_config.enable_search = memory_settings.get(
                    "enable_search", memory_config.enable_search
                )
            self.memory_manager = ConversationMemoryManager(
                memory_config, app_config=config
            )

        self._cleanup_registered = False  # Track if cleanup is registered
        if self._lightweight_ephemeral_client:
            from ..tools.registry import ToolRegistry

            self._tool_registry = ToolRegistry()
        else:
            self._tool_registry = build_runtime_tool_registry_for_client(
                build_runtime_tool_registry,
                config,
                client=self,
            )
            ensure_load_tool_pack_tool(self._tool_registry, self)

        # Initialize reasoning manager
        self.reasoning_manager = None
        if self.config and not self._lightweight_ephemeral_client:
            reasoning_config = get_config_value("reasoning", {})
            if reasoning_config.get("enabled", False):
                self.reasoning_manager = ReasoningManager(self, reasoning_config)
                logger.info(
                    "Agent reasoning enabled (threshold: %s)",
                    reasoning_config.get("complexity_threshold", 0.6),
                )

        if self._lightweight_ephemeral_client:
            self.agent = None
        else:
            self.agent = self._create_character_agent()
            logger.info(
                "Character agent initialized: %s",
                self.character_name,
            )

        # Initialize Spotify
        spotify_enabled = get_config_value("spotify", {}).get("enabled", True)
        if (
            not self._lightweight_ephemeral_client
            and self.config
            and spotify_enabled
        ):
            spotify_success = init_spotify_manager()
            if spotify_success:
                logger.info(
                    "Spotify integration initialized",
                )
            else:
                logger.warning(
                    "Spotify integration unavailable; continuing without Spotify",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
        elif (
            not self._lightweight_ephemeral_client
            and not spotify_enabled
        ):
            logger.info(
                "Spotify integration disabled by configuration",
            )

        # 常駐clientだけprocess終了hookを持つ。短命clientは所有者がcleanupする。
        if _config_bool(config, "runtime.register_process_cleanup", True):
            self._register_cleanup()

    def cloud_advisor_scope(
        self,
        *,
        origin: Any | None = None,
        assessment: Any | None = None,
    ):
        """Return a context manager for one trusted parent Cloud Advisor turn.

        Request/controller code can use this narrow seam around an async
        generation call to distinguish an explicit user request from an
        automatic, semantically assessed escalation::

            async with ...  # use ``cloud_advisor_parent_scope`` for sync
            with client.cloud_advisor_scope(origin=..., assessment=...):
                await client.generate_response_async(...)

        ``origin`` and ``assessment`` are validated by the native bridge and
        the Cloud Advisor service.  Untrusted mappings, regex/length hints, or
        model-provided booleans are ignored rather than promoted to authority.
        Existing callers should omit this context manager and retain the
        historical Main-agent/no-assessment behavior.
        """

        return cloud_advisor_parent_scope(
            origin=origin,
            assessment=assessment,
        )

    # More explicit alias for controllers that want to document that this is
    # a parent-owned scope rather than a general provider setting.
    cloud_advisor_parent_scope = cloud_advisor_scope


class TargetConfig:
    """グローバル設定を書き換えず、1実行対象だけを上書きするproxy。"""

    def __init__(self, base: Any, overrides: Dict[str, Any]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides))

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        # Deployment overlays commonly replace a nested provider mapping (for
        # example ``openai_compatible_local``).  Honour dotted reads against
        # that replacement before falling back to the persisted config.
        for prefix, value in self._overrides.items():
            if not isinstance(value, dict) or not key.startswith(f"{prefix}."):
                continue
            current: Any = value
            for part in key[len(prefix) + 1 :].split("."):
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                return current
        return _config_get(self._base, key, default)

    def set(self, key: str, value: Any) -> None:
        self._overrides[key] = value

    def get_character_config(self, character_name: str) -> Dict[str, Any]:
        """Delegate Character resolution, with a mapping-only compatibility fallback.

        Real application Config objects keep their canonical Character
        resolver. Lightweight/legacy mapping snapshots used for request-local
        target construction have never carried that method; they receive a
        generic assistant Character rather than turning a normal non-lightweight
        session reroute into an AttributeError.
        """

        resolver = getattr(self._base, "get_character_config", None)
        if callable(resolver):
            return resolver(character_name)
        name = str(character_name or "Assistant").strip() or "Assistant"
        return {
            "name": name,
            "personality": {},
        }

    def save_to_file(self, key: str, value: Any) -> bool:
        # ターン専用設定は永続化しない。
        self.set(key, value)
        return True

    def __getattr__(self, name: str) -> Any:
        if isinstance(self._base, dict):
            if name in self._base:
                return self._base[name]
            raise AttributeError(name)
        return getattr(self._base, name)


TARGET_CLIENT_PROVIDERS = {
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "sglang",
    "ollama",
    "openai_compatible_local",
    "routing-profile",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}

_LLAMA_CPP_TARGET_RUNTIME_KEYS = (
    "profile_id",
    "model_path",
    "model_alias",
    "context_size",
    "extra_args",
    "gpu_layers",
    "auto_start",
    "reasoning_effort",
    "mtp_enabled",
    "mtp_model_path",
    "mtp_supported",
    "mtp_available",
    "mtp_status",
    "mtp_reason",
    "mtp_artifact_path",
    "mtp_resolved_model_path",
    "mtp_variant_model_path",
    "mtp_mode",
)

_FREETOKEN_TARGET_RUNTIME_KEYS = (
    "host",
    "port",
    "auto_start",
    "readiness_timeout",
    "extra_args",
)

_LLAMA_CPP_FREETOKEN_MASK_KEYS = (
    "executable",
    "profile_id",
    "model_path",
    "model_root",
    "model_alias",
    "host",
    "port",
    "context_size",
    "extra_args",
    "gpu_layers",
    "auto_start",
    "readiness_timeout",
    "reasoning_effort",
    "mtp_enabled",
    "mtp_model_path",
    "mtp_supported",
    "mtp_available",
    "mtp_status",
    "mtp_reason",
    "mtp_artifact_path",
    "mtp_resolved_model_path",
    "mtp_variant_model_path",
    "mtp_mode",
)


def _target_enable_tools_option(
    options: Optional[Dict[str, Any]],
) -> Optional[bool]:
    """Return a validated per-target tool flag when one was supplied."""

    if not options or "enable_tools" not in options:
        return None
    value = options["enable_tools"]
    if not isinstance(value, bool):
        raise ValueError("provider_options.enable_tools must be a boolean")
    return value


def _overlay_openai_compatible_local_target_runtime(
    config: Any,
    overrides: Dict[str, Any],
    model: str,
    *,
    options: Optional[Dict[str, Any]] = None,
) -> None:
    """Copy persisted local settings and overlay the selected local runtime.

    The persist dict is never mutated.  ``local-model`` stays an external
    endpoint and managed runtime settings remain request-scoped.
    """

    options = options or {}
    enable_tools = _target_enable_tools_option(options)
    base_local = _config_get(config, "openai_compatible_local", {})
    if not isinstance(base_local, dict):
        base_local = {}
    local_copy = copy.deepcopy(base_local)
    target_model = str(model or "").strip()
    local_copy["model"] = target_model
    if enable_tools is not None:
        local_copy["enable_tools"] = enable_tools

    from .openai_compatible_local_profiles import managed_local_runtime_for_model

    managed_runtime = managed_local_runtime_for_model(
        config,
        target_model,
    )
    if managed_runtime == "freetoken":
        from src.service_manager._local_llm_servers import _freetoken_settings

        resolved = _freetoken_settings(
            config,
            model=target_model,
        )
        local_copy["freetoken"] = {
            key: copy.deepcopy(resolved[key])
            for key in _FREETOKEN_TARGET_RUNTIME_KEYS
            if key in resolved
        }
        local_copy["llama_cpp"] = {}
        local_copy.pop("reasoning_effort", None)
        overrides.pop("runtime.target_reasoning_effort", None)
        for key in _LLAMA_CPP_FREETOKEN_MASK_KEYS:
            overrides[
                f"openai_compatible_local.llama_cpp.{key}"
            ] = None
    elif target_model.casefold() != "local-model":
        from src.service_manager._local_llm_servers import _llama_cpp_settings
        from .openai_compatible_local_profiles import llama_cpp_reasoning_effort_metadata

        resolved = _llama_cpp_settings(config, model=target_model)
        llama_cpp = local_copy.get("llama_cpp")
        if not isinstance(llama_cpp, dict):
            llama_cpp = {}
        else:
            llama_cpp = dict(llama_cpp)
        for key in _LLAMA_CPP_TARGET_RUNTIME_KEYS:
            if key in resolved:
                llama_cpp[key] = resolved[key]
        metadata = llama_cpp_reasoning_effort_metadata(target_model)
        if metadata:
            requested_effort = str(
                overrides.get("runtime.target_reasoning_effort") or ""
            ).strip().lower()
            # ``effort`` is applied by the caller below through a target-only
            # override; a malformed value must fail closed rather than map to
            # another Qwen mode.
            if requested_effort and requested_effort not in metadata["options"]:
                raise ValueError(
                    "Unsupported reasoning effort for managed local profile: "
                    f"{requested_effort!r}; expected one of {metadata['options']}"
                )
            if requested_effort:
                llama_cpp["reasoning_effort"] = requested_effort
        local_copy["llama_cpp"] = llama_cpp
    else:
        # ``local-model`` is an operator-owned external endpoint.  Remove
        # stale managed MTP controls from the request-scoped overlay so they
        # cannot be projected back into an external target or accidentally
        # influence a later managed launch.
        llama_cpp = local_copy.get("llama_cpp")
        if isinstance(llama_cpp, dict):
            llama_cpp = dict(llama_cpp)
            for key in (
                "profile_id",
                "mtp_enabled",
                "mtp_model_path",
                "mtp_supported",
                "mtp_available",
                "mtp_status",
                "mtp_reason",
                "mtp_artifact_path",
                "mtp_resolved_model_path",
                "mtp_variant_model_path",
                "mtp_mode",
            ):
                # Explicit ``None`` prevents TargetConfig dotted reads from
                # falling through to stale persisted values while keeping the
                # external overlay free of managed MTP semantics.
                llama_cpp[key] = None
            local_copy["llama_cpp"] = llama_cpp
    overrides["openai_compatible_local"] = local_copy


def create_llm_client_for_target(
    config: Any,
    *,
    provider: str,
    model: str,
    credential_profile: Any = None,
    effort: str = "",
    base_url: str = "",
    provider_options: Optional[Dict[str, Any]] = None,
    api_key: str = "",
):
    """明示provider/model/credentialからターン専用clientを生成する。"""

    provider = str(provider or "").strip().lower()
    if provider not in TARGET_CLIENT_PROVIDERS:
        raise ValueError(f"未対応のターン専用LLM providerです: {provider or '(empty)'}")
    # Enterprise deployment constraints are checked before any provider
    # adapter is imported.  In particular, a stale persisted ``sglang``
    # selection must never start an SGLang client when the release is fixed to
    # Gemma/vLLM (or another backend).
    deployment = resolve_llm_deployment(config)
    if deployment is not None:
        preflight_deployment(
            config,
            provider=provider,
            model=model,
            base_url=base_url or None,
        )
    if provider == "sglang":
        from .sglang_url import enforce_enterprise_sglang_model

        model = enforce_enterprise_sglang_model(config, provider, model)
    options = dict(provider_options or {})
    enable_tools = _target_enable_tools_option(options)
    lightweight_client = options.get("lightweight_client", False)
    if not isinstance(lightweight_client, bool):
        raise ValueError(
            "provider_options.lightweight_client must be a boolean"
        )
    resolved_key = api_key or str(
        getattr(credential_profile, "api_key", "") or ""
    )
    resolved_base_url = base_url or str(
        getattr(credential_profile, "base_url", "") or ""
    )
    overrides: Dict[str, Any] = {
        "llm_provider": provider,
        "llm_model": model,
        "runtime.target_model": model,
        "runtime.register_process_cleanup": False,
        "conversation_state.mode": "stateless",
        # 通常利用のfallback文言は維持し、無料Teamだけrouting層へ元例外を返す。
        "free_team.propagate_errors": True,
    }
    if options.get("max_output_tokens") is not None:
        overrides["runtime.target_max_output_tokens"] = max(
            1, int(options["max_output_tokens"])
        )
    if options.get("ephemeral_session_client"):
        overrides["runtime.ephemeral_session_client"] = True
        overrides["memory.enabled"] = False
    if enable_tools is not None:
        overrides["runtime.target_enable_tools"] = enable_tools
    if "lightweight_client" in options:
        # Explicit False is significant: Project Steward must be able to mask
        # a stale/base lightweight flag while creating its tool-enabled,
        # read-only request client.
        overrides["runtime.lightweight_ephemeral_client"] = lightweight_client
    if resolved_key:
        overrides["runtime.target_api_key"] = resolved_key
    if resolved_base_url:
        overrides["runtime.target_base_url"] = resolved_base_url
    if options.get("defer_server_start"):
        overrides["runtime.defer_server_start"] = True
    if options.get("disable_server_auto_start"):
        overrides["runtime.disable_server_auto_start"] = True
    # Target-scoped tool enablement must reach every provider adapter. Some
    # engines consume provider-specific flags, while registry-backed engines
    # use `use_tools`; keep both projections request-scoped and consistent.
    if enable_tools is not None:
        overrides["use_tools"] = enable_tools

    if provider == "openai":
        if resolved_key:
            overrides["openai_api_key"] = resolved_key
        overrides["openai.model"] = model
        if effort:
            overrides["openai.reasoning_effort"] = effort
    elif provider == "gemini":
        if resolved_key:
            overrides["gemini_api_key"] = resolved_key
        overrides["gemini.model"] = model
    elif provider == "openrouter":
        if resolved_key:
            overrides["openrouter_api_key"] = resolved_key
        overrides["openrouter.model"] = model
        if resolved_base_url:
            overrides["openrouter.base_url"] = resolved_base_url
        overrides["openrouter.enable_tools"] = "tools" in set(
            options.get("capabilities") or []
        ) or bool(options.get("enable_tools", True))
        overrides["free_team.request_extra_body"] = {
            "provider": {
                "allow_fallbacks": False,
                "max_price": {"prompt": 0, "completion": 0},
            },
        }
    elif provider == "deepseek":
        if resolved_key:
            overrides["deepseek_api_key"] = resolved_key
        overrides["deepseek.model"] = model
        if resolved_base_url:
            overrides["deepseek.base_url"] = resolved_base_url
            overrides["deepseek_base_url"] = resolved_base_url
        selected_effort = str(
            effort or options.get("reasoning_effort") or "high"
        ).strip().lower()
        if selected_effort not in {"none", "high", "max"}:
            selected_effort = "high"
        overrides["deepseek.reasoning_effort"] = selected_effort
    elif provider == "deepinfra":
        if resolved_key:
            overrides["deepinfra_api_key"] = resolved_key
        overrides["deepinfra.model"] = model
        if resolved_base_url:
            overrides["deepinfra.base_url"] = resolved_base_url
            overrides["deepinfra_base_url"] = resolved_base_url
        selected_effort = str(
            effort or options.get("reasoning_effort") or "high"
        ).strip().lower()
        if selected_effort not in {"none", "low", "medium", "high"}:
            selected_effort = "high"
        overrides["deepinfra.reasoning_effort"] = selected_effort
    elif provider == "kimi":
        if resolved_key:
            overrides["kimi_api_key"] = resolved_key
        overrides["kimi.model"] = model
        if resolved_base_url:
            overrides["kimi.base_url"] = resolved_base_url
            overrides["kimi_base_url"] = resolved_base_url
        overrides["kimi.reasoning_effort"] = "max"
    elif provider == "codex-cli" and effort:
        overrides["codex_cli.reasoning_effort"] = effort
    elif provider == "claude-cli" and effort:
        overrides["claude_cli.reasoning_effort"] = effort
    elif provider == "openai_compatible_local":
        from .openai_compatible_local_profiles import (
            llama_cpp_reasoning_effort_metadata,
            managed_local_runtime_for_model,
        )

        local_runtime = managed_local_runtime_for_model(config, model)
        local_metadata = (
            None
            if local_runtime == "freetoken"
            else llama_cpp_reasoning_effort_metadata(model)
        )
        if effort and local_metadata:
            normalized_effort = str(effort).strip().lower()
            if normalized_effort not in local_metadata["options"]:
                raise ValueError(
                    "Unsupported reasoning effort for managed local profile: "
                    f"{effort!r}; expected one of {local_metadata['options']}"
                )
            overrides["runtime.target_reasoning_effort"] = normalized_effort
        _overlay_openai_compatible_local_target_runtime(
            config,
            overrides,
            model,
            options=options,
        )
    elif provider == "ollama":
        overrides["ollama.model"] = model
        if resolved_base_url:
            overrides["ollama.base_url"] = resolved_base_url
        if enable_tools is not None:
            overrides["ollama.enable_tools"] = enable_tools
    elif provider == "routing-profile":
        clean_model = str(model or "").strip()
        if clean_model != "free-team":
            raise ValueError(
                f"未対応のルーティングプロファイルです: {clean_model or '(empty)'}"
            )
        overrides["llm_provider"] = "routing-profile"
        overrides["llm_model"] = "free-team"
    if resolved_base_url and provider not in {"openrouter"}:
        overrides[f"{provider}.base_url"] = resolved_base_url
    target_config = TargetConfig(config, overrides)
    return create_llm_client(target_config)


def create_llm_client(
    config: Config,
) -> Union[AgentLLMClient, "GeminiLLMClient", "CLILLMClient", "OllamaClient"]:
    """Factory function to create LLM client

    Args:
        config: Application configuration

    Returns:
        Configured LLM client instance
    """
    # ``Config`` remains the persisted source of truth, but an Enterprise
    # deployment may expose a fixed runtime backend.  Apply only an in-memory
    # TargetConfig overlay so DB values remain available for diagnostics and
    # are not silently rewritten on startup.
    deployment = resolve_llm_deployment(config)
    if deployment is not None:
        preflight_deployment(config)
        overrides = effective_config_overrides(config)
        if overrides:
            config = TargetConfig(config, overrides)

    llm_provider = str(config.get("llm_provider", "openai") or "openai").strip().lower()
    if llm_provider not in KNOWN_PROVIDER_IDS:
        raise RuntimeError(
            f"Unsupported LLM provider '{llm_provider}'; refusing to fall back "
            "to the official OpenAI transport"
        )

    # Project a safe effective model onto this turn-scoped config.  This keeps
    # provider engines that do not call the native factory (Gemini/CLI/local)
    # from forwarding a retired persisted model.  Explicit non-retired model
    # selections remain untouched.
    if llm_provider != "routing-profile":
        safe_model = canonical_model_for_provider(config, llm_provider)
        if not safe_model or is_retired_model(safe_model):
            raise RuntimeError(
                f"No safe model is configured for provider '{llm_provider}'"
            )
        current_model = str(config.get("llm_model", "") or "").strip()
        if current_model != safe_model:
            config = TargetConfig(config, {"llm_model": safe_model})

    if llm_provider == "routing-profile":
        model = str(config.get("llm_model", "") or "")
        if model != "free-team":
            raise RuntimeError(f"未対応のルーティングプロファイルです: {model}")
        from .free_team_client import FreeTeamRoutingClient

        return FreeTeamRoutingClient(config)

    if llm_provider == "gemini":
        logger.info("Creating Gemini LLM client")
        from .gemini_engine import create_gemini_client

        return create_gemini_client(config)

    elif llm_provider == "sglang":
        logger.info("Creating SGLang LLM client")
        from .sglang_engine import create_sglang_client

        return create_sglang_client(config)

    elif llm_provider == "ollama":
        logger.info("Creating Ollama LLM client")
        from .ollama_engine import create_ollama_client

        return create_ollama_client(config)

    elif llm_provider == "openai_compatible_local":
        logger.info(
            "Creating OpenAI-compatible local LLM client",
        )
        from .openai_compatible_local_engine import (
            create_openai_compatible_local_client,
        )

        return create_openai_compatible_local_client(config)

    elif llm_provider in ["antigravity-cli", "claude-cli", "codex-cli", "grok-cli"]:
        # CLI-based providers
        logger.info(
            "Creating %s CLI LLM backend",
            llm_provider.upper(),
        )

        # Select appropriate CLI backend
        if llm_provider == "antigravity-cli":
            from .cli_backends.antigravity import AntigravityCLIBackend as CLIImpl

            cli_backend = CLIImpl(model=config.get("llm_model"))
        elif llm_provider == "claude-cli":
            from .cli_backends.claude import ClaudeCLIBackend as CLIImpl

            cli_backend = CLIImpl(
                model=config.get("llm_model"),
                reasoning_effort=config.get("claude_cli.reasoning_effort"),
            )
        elif llm_provider == "codex-cli":
            from .cli_backends.codex import CodexCLIBackend as CLIImpl

            cli_backend = CLIImpl(
                model=config.get("llm_model"),
                reasoning_effort=config.get("codex_cli.reasoning_effort"),
            )
        elif llm_provider == "grok-cli":
            from .cli_backends.grok import GrokCLIBackend as CLIImpl

            cli_backend = CLIImpl(model=config.get("llm_model"))

        from .cli_llm_client import CLILLMClient

        return CLILLMClient(config=config, cli_backend=cli_backend)

    elif llm_provider == "openrouter":
        logger.info("Creating OpenRouter LLM client")
        api_key = config.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OpenRouterを使用するには OPENROUTER_API_KEY を設定してください"
            )

        base_url = (
            config.get("openrouter.base_url")
            or config.get("openrouter_base_url")
            or os.getenv("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
        )
        site_url = (
            config.get("openrouter.site_url")
            or config.get("openrouter_site_url")
            or os.getenv("OPENROUTER_SITE_URL")
            or os.getenv("OPENROUTER_HTTP_REFERER")
        )
        app_name = (
            config.get("openrouter.app_name")
            or config.get("openrouter_app_name")
            or os.getenv("OPENROUTER_APP_NAME")
            or "AoiTalk"
        )
        headers = {}
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-Title"] = app_name

        return AgentLLMClient(
            api_key=api_key,
            model=canonical_model_for_provider(config, "openrouter"),
            config=config,
            provider_label="openrouter",
            base_url=base_url,
            default_headers=headers,
            force_chat_completions=True,
            supports_tools=(
                _config_bool(config, "openrouter.enable_tools", False)
                and _config_bool(config, "runtime.target_enable_tools", True)
            ),
        )

    elif llm_provider == "deepseek":
        logger.info("Creating DeepSeek LLM client")
        api_key = config.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DeepSeekを使用するには DEEPSEEK_API_KEY を設定してください"
            )
        base_url = (
            config.get("deepseek_base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or config.get("deepseek.base_url")
            or "https://api.deepseek.com"
        )
        return AgentLLMClient(
            api_key=api_key,
            model=(
                config.get("llm_model")
                or config.get("deepseek.model")
                or "deepseek-v4-flash"
            ),
            config=config,
            provider_label="deepseek",
            base_url=base_url,
            force_chat_completions=True,
            supports_tools=_config_bool(
                config, "runtime.target_enable_tools", True
            ),
        )

    elif llm_provider == "deepinfra":
        logger.info("Creating DeepInfra LLM client")
        api_key = config.get("deepinfra_api_key") or os.getenv("DEEPINFRA_TOKEN")
        if not api_key:
            raise RuntimeError(
                "DeepInfraを使用するには DEEPINFRA_TOKEN を設定してください"
            )
        base_url = (
            config.get("deepinfra.base_url")
            or config.get("deepinfra_base_url")
            or os.getenv("DEEPINFRA_BASE_URL")
            or "https://api.deepinfra.com/v1/openai"
        )
        return AgentLLMClient(
            api_key=api_key,
            model=(
                config.get("llm_model")
                or config.get("deepinfra.model")
                or "deepseek-ai/DeepSeek-V4-Flash"
            ),
            config=config,
            provider_label="deepinfra",
            base_url=base_url,
            force_chat_completions=True,
            supports_tools=_config_bool(
                config, "runtime.target_enable_tools", True
            ),
        )

    elif llm_provider == "kimi":
        logger.info("Creating Kimi LLM client")
        api_key = config.get("kimi_api_key") or os.getenv("MOONSHOT_API_KEY")
        if not api_key:
            raise RuntimeError("Kimiを使用するには MOONSHOT_API_KEY を設定してください")
        base_url = (
            config.get("kimi_base_url")
            or os.getenv("MOONSHOT_BASE_URL")
            or config.get("kimi.base_url")
            or "https://api.moonshot.ai/v1"
        )
        return AgentLLMClient(
            api_key=api_key,
            model=config.get("llm_model", config.get("kimi.model", "kimi-k3")),
            config=config,
            provider_label="kimi",
            base_url=base_url,
            force_chat_completions=True,
            supports_tools=_config_bool(
                config, "runtime.target_enable_tools", True
            ),
        )

    else:  # openai or default
        logger.info("Creating OpenAI LLM client")
        base_url = (
            config.get("openai.base_url")
            or config.get("openai_base_url")
            or os.getenv("OPENAI_BASE_URL")
            or None
        )
        return AgentLLMClient(
            api_key=config.get("openai_api_key"),
            model=canonical_model_for_provider(config, "openai"),
            config=config,
            base_url=base_url,
            supports_tools=_config_bool(
                config,
                "runtime.target_enable_tools",
                True,
            ),
        )
