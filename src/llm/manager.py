"""LLM client manager for OpenAI, OpenRouter, Gemini, Ollama, SGLang, and CLI backends."""

import os
import logging
import re
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
    create_async_openai_client,
)
from .provider_capabilities import ProviderCapabilities
from ..tools import init_spotify_manager
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..reasoning import ReasoningManager
from .runtime_tool_registry import build_runtime_tool_registry
from .generation_policy import DEFAULT_GENERATION_POLICY
from .conversation_context import conversation_state_mode
from .manager_parts import (
    AgentSetupMixin,
    ContextBuildingMixin,
    GenerationApiMixin,
    MemoryIntegrationMixin,
    TurnExecutionMixin,
)

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

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
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
        self.model_name = model
        self.provider_label = provider_label
        self._native_tools_enabled = bool(supports_tools)
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=self._native_tools_enabled,
            supports_response_format=provider_label == "openai",
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=False,
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
        if hasattr(config, "default_character"):
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
        self.current_session_id: Optional[str] = (
            None  # For session-specific message storage
        )
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._last_context_snapshots: list[dict[str, Any]] = []
        self._loaded_history_session_id: Optional[str] = None
        self._model_transcript: list[dict[str, Any]] = []
        self._last_model_transcript: list[dict[str, Any]] = []
        self._provider_state_mode = conversation_state_mode(config, provider_label)
        self._provider_state = {"previous_response_id": None, "fingerprint": None}

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

        self._memory_enabled = get_config_value("memory", {}).get("enabled", True)
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
                memory_config.embedding_model = memory_settings.get(
                    "embedding_model", memory_config.embedding_model
                )
                memory_config.preload_embedding_model = memory_settings.get(
                    "preload_embedding_model", memory_config.preload_embedding_model
                )
                memory_config.enable_search = memory_settings.get(
                    "enable_search", memory_config.enable_search
                )
            self.memory_manager = ConversationMemoryManager(
                memory_config, app_config=config
            )

        self._cleanup_registered = False  # Track if cleanup is registered
        self._tool_registry = build_runtime_tool_registry(config)

        # Initialize reasoning manager
        self.reasoning_manager = None
        if self.config:
            reasoning_config = get_config_value("reasoning", {})
            if reasoning_config.get("enabled", False):
                self.reasoning_manager = ReasoningManager(self, reasoning_config)
                print(
                    f"[AgentLLMClient] Reasoning enabled (threshold: {reasoning_config.get('complexity_threshold', 0.6)})"
                )

        self.agent = self._create_character_agent()
        print(f"[AgentLLMClient] Character agent initialized: {self.character_name}")

        # Initialize Spotify
        spotify_enabled = get_config_value("spotify", {}).get("enabled", True)
        if self.config and spotify_enabled:
            spotify_success = init_spotify_manager()
            if spotify_success:
                print("[AgentLLMClient] Spotify initialized successfully")
            else:
                print(
                    "[AgentLLMClient] Spotify initialization failed; continuing without Spotify"
                )
        elif not spotify_enabled:
            print("[AgentLLMClient] Spotify feature is disabled")

        # Register cleanup handler
        self._register_cleanup()


class TargetConfig:
    """グローバル設定を書き換えず、1実行対象だけを上書きするproxy。"""

    def __init__(self, base: Any, overrides: Dict[str, Any]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides))

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return _config_get(self._base, key, default)

    def set(self, key: str, value: Any) -> None:
        self._overrides[key] = value

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

    options = dict(provider_options or {})
    resolved_key = api_key or str(
        getattr(credential_profile, "api_key", "") or ""
    )
    resolved_base_url = base_url or str(
        getattr(credential_profile, "base_url", "") or ""
    )
    overrides: Dict[str, Any] = {
        "llm_provider": provider,
        "llm_model": model,
        "conversation_state.mode": "stateless",
        "free_team.max_output_tokens": max(
            1, int(options.get("max_output_tokens") or 2048)
        ),
        # 通常利用のfallback文言は維持し、無料Teamだけrouting層へ元例外を返す。
        "free_team.propagate_errors": True,
    }
    if provider == "openai":
        overrides["openai_api_key"] = resolved_key
        overrides["openai.model"] = model
        if effort:
            overrides["openai.reasoning_effort"] = effort
    elif provider == "gemini":
        overrides["gemini_api_key"] = resolved_key
        overrides["gemini.model"] = model
    elif provider == "openrouter":
        overrides["openrouter_api_key"] = resolved_key
        overrides["openrouter.model"] = model
        overrides["openrouter.base_url"] = (
            resolved_base_url or "https://openrouter.ai/api/v1"
        )
        overrides["openrouter.enable_tools"] = "tools" in set(
            options.get("capabilities") or []
        ) or bool(options.get("enable_tools", True))
        overrides["free_team.request_extra_body"] = {
            "provider": {
                "allow_fallbacks": False,
                "max_price": {"prompt": 0, "completion": 0},
            },
        }
    elif provider == "kimi":
        overrides["kimi_api_key"] = resolved_key
        overrides["kimi.model"] = model
        overrides["kimi.base_url"] = (
            resolved_base_url or "https://api.moonshot.ai/v1"
        )
        overrides["kimi.reasoning_effort"] = "max"
    elif provider == "codex-cli" and effort:
        overrides["codex_cli.reasoning_effort"] = effort
    elif provider == "claude-cli" and effort:
        overrides["claude_cli.reasoning_effort"] = effort
    if resolved_base_url and provider not in {"openrouter"}:
        overrides[f"{provider}.base_url"] = resolved_base_url
    options["max_output_tokens"] = overrides["free_team.max_output_tokens"]
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
    llm_provider = config.get("llm_provider", "openai").lower()

    if llm_provider == "routing-profile":
        model = str(config.get("llm_model", "") or "")
        if model != "free-team":
            raise RuntimeError(f"未対応のルーティングプロファイルです: {model}")
        from .free_team_client import FreeTeamRoutingClient

        return FreeTeamRoutingClient(config)

    if llm_provider == "gemini":
        print(f"[LLM Factory] Geminiクライアントを作成")
        from .gemini_engine import create_gemini_client

        return create_gemini_client(config)

    elif llm_provider == "sglang":
        print(f"[LLM Factory] SGLangクライアントを作成")
        from .sglang_engine import create_sglang_client

        return create_sglang_client(config)

    elif llm_provider == "ollama":
        print("[LLM Factory] Ollamaクライアントを作成")
        from .ollama_engine import create_ollama_client

        return create_ollama_client(config)

    elif llm_provider == "openai_compatible_local":
        print(f"[LLM Factory] OpenAI互換ローカルクライアントを作成")
        from .openai_compatible_local_engine import (
            create_openai_compatible_local_client,
        )

        return create_openai_compatible_local_client(config)

    elif llm_provider in ["antigravity-cli", "claude-cli", "codex-cli", "grok-cli"]:
        # CLI-based providers
        print(f"[LLM Factory] {llm_provider.upper()} Backendを作成")

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
        print(f"[LLM Factory] OpenRouter Agentクライアントを作成")
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
            model=config.get(
                "llm_model",
                config.get("openrouter.model", "openai/gpt-4o-mini"),
            ),
            config=config,
            provider_label="openrouter",
            base_url=base_url,
            default_headers=headers,
            force_chat_completions=True,
            supports_tools=_config_bool(config, "openrouter.enable_tools", False),
        )

    elif llm_provider == "kimi":
        print("[LLM Factory] Kimi APIクライアントを作成")
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
            supports_tools=True,
        )

    else:  # openai or default
        print(f"[LLM Factory] OpenAI Agentクライアントを作成")
        return AgentLLMClient(
            api_key=config.get("openai_api_key"),
            model=config.get("llm_model", "gpt-4o"),
            config=config,
        )
