"""LLM client manager for OpenAI, OpenRouter, Gemini, Ollama, SGLang, and CLI backends."""

import asyncio
import concurrent.futures
import inspect
import json
import os
import logging
import re
from typing import Optional, List, Dict, Any, Union, Generator, Callable, Awaitable

from ..config import Config
from ..services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    get_runtime_project_context,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from ..services.scenario_chat_context import (
    build_scenario_chat_context,
    is_scenario_workflow_tool_allowed,
)
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
from .prompts import build_unified_instructions
from ..reasoning import ReasoningManager
from .runtime_tool_registry import build_runtime_tool_registry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .agentic_completion import (
    agentic_completion_enabled,
    agentic_max_rounds,
    build_agentic_continuation_context,
    build_agentic_review_prompt,
    parse_agentic_review_decision,
    run_agentic_completion_loop_async,
)
from .agent_runtime import build_tool_hint_context_async
from .specialist_delegate import (
    reset_runtime_specialist_provider,
    set_runtime_specialist_provider,
)
from .tool_policy import (
    command_capability_active,
    looks_like_bare_search_followup_request,
    project_progress_review_active,
    reset_current_user_input,
    set_current_user_input,
)
from ..services.user_settings_service import get_user_custom_instructions_sync

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


class AgentLLMClient:
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
        self._loaded_history_session_id: Optional[str] = None

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
                memory_config.llm_provider = get_config_value("llm_provider", "gemini")
                memory_config.llm_model = get_config_value(
                    "llm_model", "gemini-3-flash-preview"
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

    def _build_instructions(self) -> str:
        """統一的なシステムプロンプトを生成（共通関数を使用）"""
        # セッションに紐づくRPステアリング設定を取得
        rp_settings = self._get_current_rp_settings()
        return build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            rp_settings=rp_settings,
            custom_instructions=get_user_custom_instructions_sync(
                self._get_session_user_id()
            ),
        )

    def _get_current_rp_settings(self) -> Optional[dict]:
        """現在のセッションのRPステアリング設定を取得する。"""
        if not self.current_session_id:
            return None
        try:
            from ..memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            session = self._run_sync(repo.get_session_by_id(self.current_session_id))
            if session and hasattr(session, "rp_settings"):
                return session.rp_settings or None
        except Exception as e:
            logger.warning(f"RPステアリング設定の取得に失敗: {e}")
        return None

    def _build_effective_instructions(self, scenario_chat_context=None) -> str:
        if scenario_chat_context:
            return scenario_chat_context.prompt
        return self._build_instructions()

    def _create_character_agent(self) -> Agent:
        """Create character agent with tools from the unified runtime registry."""
        base_tools = (
            self._tool_registry.get_all()
            if getattr(self, "_native_tools_enabled", True)
            else []
        )

        # キャラクター名を決定
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            character_name = character_config.get("name", self.character_name)
        else:
            character_name = "MainAssistant"

        agent_kwargs: Dict[str, Any] = {}
        reasoning_effort = self._get_reasoning_effort()
        if reasoning_effort:
            agent_kwargs["model_settings"] = ModelSettings(
                reasoning=Reasoning(effort=reasoning_effort)
            )

        return Agent(
            name=character_name,
            instructions=self._build_effective_instructions(),
            tools=base_tools,
            model=self.model_name,
            **agent_kwargs,
        )

    def _get_reasoning_effort(self) -> Optional[str]:
        if self.provider_label != "openai" or not self.config:
            return None
        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        effort = str(self.config.get("openai.reasoning_effort", "") or "").strip()
        if not effort:
            return None
        if effort not in reasoning_effort_options_for_model("openai", self.model_name):
            return None
        return effort

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        prompt = "\n".join(
            f"{message.get('role', 'user')}: {message.get('content', '')}"
            for message in messages
        )
        return self.generate_response(prompt, stream=False)

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        result = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        yield result

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": self.model_name}]

    def health_check(self) -> Dict[str, Any]:
        return {"ok": True, "provider": self.provider_label, "model": self.model_name}

    def _get_effective_tools_for_current_session(self, scenario_chat_context=None):
        if not getattr(self, "_native_tools_enabled", True):
            return []
        scenario_chat_context = (
            scenario_chat_context or self._get_scenario_chat_context_sync()
        )
        if not scenario_chat_context:
            return self.agent.tools
        return [
            tool
            for tool in self.agent.tools
            if is_scenario_workflow_tool_allowed(
                str(getattr(tool, "name", getattr(tool, "__name__", ""))),
                scenario_chat_context,
            )
        ]

    async def _sync_history_with_current_session(self) -> None:
        """Load persisted session messages before building prompt context."""
        session_id = self.current_session_id
        if not session_id:
            if self._loaded_history_session_id is not None:
                self.history_manager.clear()
                self._loaded_history_session_id = None
            return

        if session_id == self._loaded_history_session_id:
            return

        self.history_manager.clear()

        if not self.memory_manager or not self._memory_enabled:
            self._loaded_history_session_id = session_id
            return

        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()

        try:
            messages = await self.memory_manager.repository.get_session_messages(
                session_id
            )
        except Exception as e:
            self._loaded_history_session_id = None
            print(f"[AgentLLMClient] Failed to load session history: {e}")
            return

        max_messages = getattr(self.history_manager, "hard_limit", 100)
        for msg in messages[-max_messages:]:
            if msg.role not in {"user", "assistant", "system"}:
                continue
            self.history_manager.add_message(msg.role, msg.content)

        self._loaded_history_session_id = session_id
        print(
            f"[AgentLLMClient] Loaded {len(messages)} persisted messages for session: {session_id}"
        )

    def _build_conversation_context(self) -> str:
        """Build conversation context from history"""
        history = self.history_manager.get_all()
        scenario_chat_context = self._get_scenario_chat_context_sync()
        context_builder_block = (
            self._current_context_bundle.render_for_prompt()
            if not scenario_chat_context and self._current_context_bundle
            else ""
        )
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        )
        project_context = (
            None
            if scenario_chat_context or not include_project_context
            else get_runtime_project_context()
        )
        project_block = (
            format_project_context_for_chat_prompt(project_context)
            if project_context and not context_builder_block
            else ""
        )

        # Scenario workflow sessions use scenario_chat_context.prompt as agent
        # instructions, not as ordinary conversation context.
        scenario_block = ""
        if self.current_session_id:
            try:
                from ..services.scenario_service import (
                    get_play_session_by_conversation_id,
                )

                if not scenario_chat_context:
                    play_session = self._run_sync(
                        get_play_session_by_conversation_id(self.current_session_id)
                    )
                else:
                    play_session = None
                if play_session:
                    import json

                    scenario_data = {
                        "scenario_title": play_session.get("scenario", {}).get("title"),
                        "current_scene": play_session.get("current_scene", {}).get(
                            "title"
                        ),
                        "player_state": play_session.get("player_state", {}),
                        "status": play_session.get("status"),
                    }
                    scenario_block = f"## Active TRPG Scenario State:\n{json.dumps(scenario_data, ensure_ascii=False, indent=2)}\n"
            except Exception as e:
                print(f"[AgentLLMClient] Failed to get scenario state: {e}")

        # ワールドブック情報の取得
        worldbook_block = ""
        if self.character_name and not scenario_chat_context:
            try:
                from ..services.worldbook_service import get_matching_entries

                recent_text = (
                    " ".join(msg["content"] for msg in history[-5:]) if history else ""
                )
                entries = self._run_sync(
                    get_matching_entries(self.character_name, recent_text)
                )
                if entries:
                    lines = [
                        (
                            f"### {e['name']}\n{e['content']}"
                            if e.get("name")
                            else e["content"]
                        )
                        for e in entries
                    ]
                    worldbook_block = "## 世界情報:\n" + "\n\n".join(lines)
            except Exception as e:
                print(f"[AgentLLMClient] Failed to get worldbook: {e}")

        if not history:
            parts = [
                p
                for p in [
                    context_builder_block,
                    project_block,
                    scenario_block,
                    worldbook_block,
                ]
                if p
            ]
            return "\n\n".join(parts) if parts else ""

        current_input = history[-1]["content"]

        if len(history) == 1:
            parts = [
                p
                for p in [
                    context_builder_block,
                    project_block,
                    scenario_block,
                    worldbook_block,
                    current_input,
                ]
                if p
            ]
            return "\n\n".join(parts)

        # Get context window size from manager
        context_window = self.history_manager.context_window_size

        # Original logic: history[-11:-1] -> up to 10 items before the last one
        relevant_history = history[-(context_window + 1) : -1]

        context_parts = []
        for msg in relevant_history:
            if msg["role"] == "user":
                context_parts.append(f"ユーザー: {msg['content']}")
            else:
                context_parts.append(f"アシスタント: {msg['content']}")

        if context_parts:
            context = (
                f"過去の会話:\n"
                + "\n".join(context_parts)
                + f"\n\n現在の質問: {current_input}"
            )
        else:
            context = f"現在の質問: {current_input}"

        parts = [
            p
            for p in [
                context_builder_block,
                project_block,
                scenario_block,
                worldbook_block,
                context,
            ]
            if p
        ]
        return "\n\n".join(parts)

    def _run_sync(self, coro):
        """async コルーチンを同期的に実行するヘルパー。"""
        import concurrent.futures

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()

    def _get_scenario_chat_context_sync(self):
        if not self.current_session_id:
            return None
        try:
            return self._run_sync(build_scenario_chat_context(self.current_session_id))
        except Exception as e:
            print(f"[AgentLLMClient] Failed to resolve scenario chat context: {e}")
            return None

    async def _build_context_bundle_for_prompt(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if self._get_scenario_chat_context_sync():
            return None
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        ) and not looks_like_bare_search_followup_request(user_input)
        try:
            return await ContextBuilder().build_context(
                user_id=self._get_session_user_id(),
                message=user_input,
                project_id=self.current_project_id if include_project_context else None,
                session_id=self.current_session_id,
                project_context=project_context if include_project_context else None,
                include_project_context=include_project_context,
            )
        except Exception as e:
            print(f"[AgentLLMClient] ContextBuilder failed; no memory context injected: {e}")
            return None

    def set_character(self, character_name: str):
        """Set character and recreate main agent

        Args:
            character_name: Name of the character
        """
        self.character_name = character_name
        self.agent = self._create_character_agent()

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
                # Clear conversation history when switching characters
                self.clear_history()
                self.agent = self._create_character_agent()
                print(
                    f"[AgentLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)"
                )
            else:
                print(
                    f"[AgentLLMClient] キャラクター設定が見つかりません: {yaml_filename}"
                )
        else:
            print("[AgentLLMClient] 設定オブジェクトがありません")

    def set_system_prompt(self, prompt: str):
        """Set system prompt by recreating agent with new instructions

        Args:
            prompt: System prompt
        """
        # Since the agent is already created with character-specific instructions,
        # we recreate it with the new prompt if needed
        # For now, this is a no-op as the agent already has character instructions
        pass

    def set_llm_mode(self, mode: str):
        """Set LLM response mode

        Args:
            mode: 'fast' for quick responses, 'thinking' for deeper reasoning

        Note: This is used for response-mode providers such as SGLang/Qwen3,
              and for OpenAI reasoning effort when the active model supports it.
        """
        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        if mode in reasoning_effort_options_for_model("openai", self.model_name):
            self._current_llm_mode = mode
            if self.config:
                try:
                    self.config.set("openai.reasoning_effort", mode)
                except Exception:
                    pass
            self.agent = self._create_character_agent()
            print(f"[AgentLLMClient] Reasoning effort set to: {mode}")
            return

        self._current_llm_mode = mode
        print(f"[AgentLLMClient] LLM mode set to: {mode}")

    def get_llm_mode(self) -> str:
        """Get current LLM response mode

        Returns:
            Current mode ('fast' or 'thinking')
        """
        return getattr(self, "_current_llm_mode", "fast")

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: dict = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> Union[str, Generator[str, None, None]]:
        """Generate response using the AoiTalk-native agent runtime

        Args:
            user_input: User's input text
            temperature: Sampling temperature.
            max_tokens: Maximum tokens.
            stream: Whether to stream.
            image_data: Optional image data.
            stream_callback: Async callback for streaming events

        Returns:
            Generated response
        """
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._run_async_safe, user_input, stream_callback
                )
                response = future.result(timeout=300)

            print(f"[AgentLLMClient] 応答: {response}")

            if stream:

                def response_generator():
                    yield response

                return response_generator()
            return response

        except concurrent.futures.TimeoutError:
            print(f"[AgentLLMClient] タイムアウトエラー")
            personality = (
                self.config.get_character_config(self.character_name).get(
                    "personality", {}
                )
                if self.config
                else {}
            )
            return personality.get("fallbackReply", "エラーが発生しました")

        except Exception as e:
            print(f"[AgentLLMClient] エラー: {e}")
            import traceback

            traceback.print_exc()
            personality = (
                self.config.get_character_config(self.character_name).get(
                    "personality", {}
                )
                if self.config
                else {}
            )
            return personality.get("fallbackReply", "エラーが発生しました")

    def set_session_context(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ):
        """Update session identifiers used for memory logging."""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def _get_memory_metadata(self) -> Dict[str, Any]:
        return self.session_metadata.copy() if self.session_metadata else {}

    def _run_async_safe(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        """Safely run async code in a new event loop"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            return loop.run_until_complete(
                self._generate_async(
                    user_input,
                    stream_callback=stream_callback,
                    steering_callback=steering_callback,
                )
            )
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def _consume_steering_instructions(
        self, steering_callback: Optional[SteeringCallback]
    ) -> List[str]:
        if not steering_callback:
            return []
        try:
            result = steering_callback()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                result = [result]
            if not isinstance(result, list):
                return []
            return [str(item).strip() for item in result if str(item).strip()]
        except Exception as exc:
            print(f"[AgentLLMClient] 追加指示の取得に失敗しました: {exc}")
            return []

    async def _append_pending_steering_to_context(
        self,
        context: str,
        steering_callback: Optional[SteeringCallback],
        stream_callback: Optional[StreamCallback],
    ) -> str:
        instructions = await self._consume_steering_instructions(steering_callback)
        if not instructions:
            return context
        if stream_callback:
            await stream_callback(
                "status_update",
                {
                    "status": "steering_applied",
                    "message": "追加指示を反映します",
                },
            )
        instruction_block = "\n".join(f"- {instruction}" for instruction in instructions)
        return f"{context}\n\n追加指示:\n{instruction_block}"

    def _agentic_completion_enabled(self, user_input: str | None = None) -> bool:
        if not getattr(self, "_native_tools_enabled", True):
            return False
        return agentic_completion_enabled(self, user_input)

    def _agentic_max_rounds(self, user_input: str | None = None) -> int:
        return agentic_max_rounds(self, user_input)

    def _parse_agentic_review_decision(self, content: str) -> dict[str, str]:
        return parse_agentic_review_decision(content)

    def _build_agentic_review_prompt(
        self,
        *,
        original_context: str,
        latest_response: str,
        round_index: int,
        user_input: str | None = None,
    ) -> str:
        return build_agentic_review_prompt(
            original_context=original_context,
            latest_response=latest_response,
            round_index=round_index,
            user_input=user_input,
        )

    def _build_agentic_continuation_context(
        self,
        *,
        original_context: str,
        latest_response: str,
        decision: dict[str, str],
    ) -> str:
        return build_agentic_continuation_context(
            original_context=original_context,
            latest_response=latest_response,
            decision=decision,
        )

    async def _run_once_with_agent(
        self,
        agent: Agent,
        context: str,
        stream_callback: Optional[StreamCallback] = None,
        required_tool_name: Optional[str] = None,
    ) -> str:
        previous_max_tool_rounds = getattr(self._turn_runner, "max_tool_rounds", None)
        if previous_max_tool_rounds is not None:
            self._turn_runner.max_tool_rounds = max(
                previous_max_tool_rounds,
                self._agentic_max_rounds(context),
            )
        try:
            result = await self._turn_runner.run(
                agent,
                context,
                stream_callback=stream_callback,
            )
        finally:
            if previous_max_tool_rounds is not None:
                self._turn_runner.max_tool_rounds = previous_max_tool_rounds
        if result.tool_calls:
            print(f"[AgentLLMClient] Tool messages found: {len(result.tool_calls)}")
        if required_tool_name and not any(
            record.tool == required_tool_name and record.successful
            for record in result.tool_calls
        ):
            logger.warning(
                "[AgentLLMClient] required tool %s was not called",
                required_tool_name,
            )
            return (
                "ツール実行の検証に失敗しました: "
                f"必須ツール `{required_tool_name}` が実行されませんでした。"
            )
        return str(result.final_output or "")

    def _required_command_tool_name(self, user_input: str) -> Optional[str]:
        if "web_search" in set(
            getattr(self, "current_command_capabilities", ()) or ()
        ):
            return "web_search"
        if command_capability_active(user_input, "web_search"):
            return "web_search"
        return None

    def _agent_requiring_tool(self, agent: Agent, required_tool_name: str) -> Agent:
        required_tools = [
            tool
            for tool in agent.tools
            if str(getattr(tool, "name", "")) == required_tool_name
        ]
        return Agent(
            name=agent.name,
            instructions=agent.instructions,
            tools=required_tools,
            model=agent.model,
            model_settings=ModelSettings(
                tool_choice="required",
                reasoning=agent.model_settings.reasoning,
            ),
        )

    async def _build_tool_hint_context(self, user_input: str) -> str:
        if not getattr(self, "_native_tools_enabled", True):
            return ""
        return await build_tool_hint_context_async(
            user_input=user_input,
            registry=self._tool_registry,
            policy=get_client_generation_policy(self),
            log_prefix="AgentLLMClient",
        )

    async def _run_agentic_completion_loop(
        self,
        agent: Agent,
        context: str,
        stream_callback: Optional[StreamCallback] = None,
        user_input: str | None = None,
    ) -> str:
        if not self._agentic_completion_enabled(user_input):
            return await self._run_once_with_agent(agent, context, stream_callback)

        async def _run_once(prompt: str) -> str:
            return await self._run_once_with_agent(agent, prompt, None)

        return await run_agentic_completion_loop_async(
            client=self,
            run_once=_run_once,
            context=context,
            stream_callback=stream_callback,
            user_input=user_input,
        )

    async def _generate_async(
        self,
        user_input: str,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        """Generate response asynchronously using character agent with tools"""
        project_token = None
        specialist_provider_token = set_runtime_specialist_provider(
            self.provider_label
        )
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )

        try:
            project_context = await self._resolve_project_context()
            project_token = set_runtime_project_context(project_context)
            self._current_context_bundle = await self._build_context_bundle_for_prompt(
                user_input, project_context
            )

            if self.memory_manager and not self.memory_manager.is_initialized():
                await self.memory_manager.initialize()

            await self._sync_history_with_current_session()

            external_persistence = bool(
                getattr(self, "external_persistence_enabled", False)
            )

            if self.memory_manager and not external_persistence:
                try:
                    if self.current_session_id:
                        await self.memory_manager.add_message_to_session(
                            session_id=self.current_session_id,
                            role="user",
                            content=user_input,
                            metadata=self._get_memory_metadata(),
                            branch_from_message_id=getattr(
                                self, "current_edit_message_id", None
                            ),
                        )
                except Exception as e:
                    print(
                        f"[AgentLLMClient] Failed to save user message to memory: {e}"
                    )

            self.history_manager.add_message("user", user_input)

            context = self._build_conversation_context()
            tool_hint_context = await self._build_tool_hint_context(
                user_input
            )
            if tool_hint_context:
                context = (
                    f"{tool_hint_context}\n\n{context}"
                    if context
                    else tool_hint_context
                )
            response: Optional[str] = None
            required_tool_name = self._required_command_tool_name(user_input)

            if (
                not required_tool_name
                and self.reasoning_manager
                and self.reasoning_manager.is_reasoning_required(
                    user_input,
                    self._get_available_tools(),
                )
            ):
                print("[AgentLLMClient] Entering reasoning mode")

                progress_callback = stream_callback
                if stream_callback:
                    await stream_callback(
                        "stream_start",
                        {
                            "status": "reasoning",
                            "message": "方針を整理しています",
                        },
                    )

                response = await self.reasoning_manager.execute_reasoning_mode(
                    user_input=user_input,
                    context={
                        "available_tools": self._get_available_tools(),
                        "conversation_history": self.history_manager.get_all(),
                        "character_name": self.character_name,
                        "project_context": sanitize_project_context_for_chat(
                            get_runtime_project_context()
                        ),
                        "runtime_context": (
                            "\n\n".join(
                                part
                                for part in [
                                    tool_hint_context,
                                    (
                                        self._current_context_bundle.render_for_prompt()
                                        if self._current_context_bundle
                                        else ""
                                    ),
                                ]
                                if part
                            )
                        ),
                    },
                    progress_callback=progress_callback,
                    steering_callback=steering_callback,
                )

                if stream_callback:
                    await stream_callback("stream_end", {"content": response})

                if self.memory_manager and not external_persistence:
                    try:
                        if self.current_session_id:
                            await self.memory_manager.add_message_to_session(
                                session_id=self.current_session_id,
                                role="assistant",
                                content=response,
                                metadata=self._get_memory_metadata(),
                            )
                    except Exception as e:
                        print(
                            f"[AgentLLMClient] Failed to save assistant message to memory: {e}"
                        )

            if response is not None:
                self.history_manager.add_message("assistant", response)
                return response

            try:
                scenario_chat_context = self._get_scenario_chat_context_sync()
                effective_agent = Agent(
                    name=(
                        scenario_chat_context.agent_name
                        if scenario_chat_context
                        else self.agent.name
                    ),
                    instructions=self._build_effective_instructions(
                        scenario_chat_context
                    ),
                    tools=self._get_effective_tools_for_current_session(
                        scenario_chat_context
                    ),
                    model=self.model_name,
                    model_settings=self.agent.model_settings,
                )
                if required_tool_name:
                    effective_agent = self._agent_requiring_tool(
                        effective_agent,
                        required_tool_name,
                    )
                context = await self._append_pending_steering_to_context(
                    context, steering_callback, stream_callback
                )
                if required_tool_name:
                    response = await self._run_once_with_agent(
                        effective_agent,
                        context,
                        stream_callback,
                        required_tool_name=required_tool_name,
                    )
                else:
                    response = await self._run_agentic_completion_loop(
                        effective_agent,
                        context,
                        stream_callback,
                        user_input=user_input,
                    )
            except Exception as e:
                print(f"[AgentLLMClient] native runtime実行エラー: {e}")
                import traceback

                traceback.print_exc()
                raise

            image_event = None
            scene_description = self._extract_scene_description(response or "")
            if scene_description:
                visible_response = self._strip_scene_description_markers(response or "")
                image_event = await self._generate_scene_image_async(scene_description)
                response = visible_response or response
                if image_event and image_event.get("tag"):
                    response = f"{response}\n\n{image_event['tag']}".strip()
                    if stream_callback:
                        await stream_callback("generated_image", image_event)

            if self.memory_manager and not external_persistence:
                try:
                    if self.current_session_id:
                        await self.memory_manager.add_message_to_session(
                            session_id=self.current_session_id,
                            role="assistant",
                            content=response,
                            metadata=self._get_memory_metadata(),
                        )
                except Exception as e:
                    print(
                        f"[AgentLLMClient] Failed to save assistant message to memory: {e}"
                    )

            self.history_manager.add_message("assistant", response)

            self.check_and_summarize_history(self.history_manager)

            return response
        finally:
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            reset_runtime_specialist_provider(specialist_provider_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            self._current_context_bundle = None

    async def _run_streamed_with_callback(
        self, agent: Agent, context: str, callback: StreamCallback
    ) -> str:
        """Native runtime streaming callback bridge."""
        result = await self._turn_runner.run(
            agent,
            context,
            stream_callback=callback,
        )
        return result.final_output

    def _extract_stream_tool_call_id(self, item: Any) -> str | None:
        raw_item = getattr(item, "raw_item", None)
        call_id = getattr(raw_item, "call_id", None)
        if call_id:
            return str(call_id)
        if isinstance(raw_item, dict) and raw_item.get("call_id"):
            return str(raw_item["call_id"])
        return None

    def _extract_stream_tool_output_call_id(
        self, item: Any
    ) -> str | None:
        raw_item = getattr(item, "raw_item", None)
        call_id = getattr(raw_item, "call_id", None)
        if call_id:
            return str(call_id)
        if isinstance(raw_item, dict) and raw_item.get("call_id"):
            return str(raw_item["call_id"])
        return None

    def _extract_stream_tool_name(self, item: Any) -> str:
        raw_item = getattr(item, "raw_item", None)
        for attr in ("name", "tool_name"):
            value = getattr(raw_item, attr, None)
            if value:
                return str(value)
        function = getattr(raw_item, "function", None)
        function_name = getattr(function, "name", None)
        if function_name:
            return str(function_name)
        if isinstance(raw_item, dict):
            for key in ("name", "tool_name"):
                value = raw_item.get(key)
                if value:
                    return str(value)
            function_data = raw_item.get("function")
            if isinstance(function_data, dict) and function_data.get("name"):
                return str(function_data["name"])
        return "tool"

    def _extract_stream_tool_arguments(
        self, item: Any
    ) -> dict[str, Any] | None:
        raw_item = getattr(item, "raw_item", None)
        arguments = getattr(raw_item, "arguments", None)
        if arguments is None and isinstance(raw_item, dict):
            arguments = raw_item.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            try:
                parsed = json.loads(arguments)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return {"raw": arguments}
        if isinstance(arguments, dict):
            return arguments
        return None

    def _stringify_tool_output(self, output: Any) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False, default=str)
        except Exception:
            return str(output)

    def _build_search_tool_result(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        output_text: str,
    ) -> dict[str, Any] | None:
        if "search" not in tool_name.lower() or not output_text.strip():
            return None

        urls: list[str] = []
        for match in _SEARCH_TOOL_URL_RE.finditer(output_text):
            url = match.group(0).rstrip(".,;:!?")
            if url not in urls:
                urls.append(url)
            if len(urls) >= _SEARCH_URL_LIMIT:
                break

        query = None
        if tool_args:
            for key in ("query", "q", "search_query", "keyword", "keywords"):
                value = tool_args.get(key)
                if isinstance(value, str) and value.strip():
                    query = value.strip()
                    break

        clipped_output = output_text.strip()
        truncated = len(clipped_output) > _SEARCH_OUTPUT_LIMIT
        if truncated:
            clipped_output = clipped_output[:_SEARCH_OUTPUT_LIMIT].rstrip() + "\n...(省略)"

        return {
            "tool": tool_name,
            "query": query,
            "urls": urls,
            "output": clipped_output,
            "truncated": truncated,
        }

    def _extract_scene_description(self, response: str) -> Optional[str]:
        """応答から画像生成用のシーン描写を抽出する。"""
        import re

        match = re.search(r"\[SCENE_DESCRIPTION:\s*(.+?)\]", response, re.DOTALL)
        if not match:
            return None

        scene_description = match.group(1).strip()
        return scene_description or None

    def _strip_scene_description_markers(self, response: str) -> str:
        """表示・保存する応答から画像生成マーカーを除去する。"""
        import re

        return re.sub(r"\n?\[SCENE_DESCRIPTION:\s*.+?\]\s*", "", response, flags=re.DOTALL).strip()

    async def _generate_scene_image_async(
        self, scene_description: str
    ) -> Optional[Dict[str, Any]]:
        """シーン画像を生成し、表示用タグと配信データを返す。"""
        try:
            # キャラクターの外見タグを取得
            appearance_tags = ""
            negative_tags = ""
            comfyui_overrides: Dict[str, Any] = {}
            try:
                from ..services.character_service import get_character_for_prompt

                char_data = await get_character_for_prompt(self.character_name)
                if char_data:
                    appearance_tags = char_data.get("appearance_tags", "")
                    negative_tags = char_data.get("negative_tags", "")
                    comfyui_overrides = char_data.get("comfyui_config", {}) or {}
            except Exception as e:
                logger.warning(f"外見タグ取得エラー: {e}")

            from ..services.image_prompt_builder import build_image_prompt

            prompt, default_negative = await build_image_prompt(
                self.history_manager.get_all(),
                appearance_tags,
                scene_description,
            )
            negative_parts = [p for p in [negative_tags, default_negative] if p]
            combined_negative = ", ".join(negative_parts)

            logger.info(
                f"[AgentLLMClient] シーン画像生成開始: {prompt[:80]}..."
            )

            # 画像生成エンジンに委譲
            try:
                from ..services.comfyui_service import generate_image

                result = await generate_image(
                    prompt=prompt,
                    negative_prompt=combined_negative,
                    overrides=comfyui_overrides,
                )
                if result and result.get("success"):
                    image_path = result.get("image_path")
                    tag = f"[GENERATED_IMAGE:{image_path}]"
                    logger.info(f"[AgentLLMClient] シーン画像生成完了: {image_path}")
                    return {
                        "content": tag,
                        "tag": tag,
                        "image_path": image_path,
                        "image_url": result.get("image_url"),
                        "filename": result.get("filename"),
                    }
            except ImportError:
                logger.info(
                    "[AgentLLMClient] ComfyUI サービスが利用できません。画像生成スキップ。"
                )
            except Exception as e:
                logger.warning(f"画像生成エラー: {e}")
            return None

        except Exception as e:
            logger.error(f"[AgentLLMClient] シーン画像生成タスクエラー: {e}")
            return None

    async def _resolve_project_context(self) -> Optional[dict[str, Any]]:
        if not self.current_project_id and not self.current_session_id:
            return None

        if self.current_session_id:
            try:
                if await build_scenario_chat_context(self.current_session_id):
                    return None
            except Exception as e:
                print(f"[AgentLLMClient] Failed to resolve scenario chat context: {e}")

        resolver = ProjectContextResolver()
        try:
            return await resolver.resolve_context(
                project_id=self.current_project_id,
                session_id=self.current_session_id,
            )
        except Exception as e:
            print(f"[AgentLLMClient] Failed to resolve project context: {e}")
            return None

    def check_and_summarize_history(self, history_manager=None) -> None:
        """Check if history needs summarization and start background task.

        Args:
            history_manager: HistoryManager instance to check. Defaults to self.history_manager.
        """
        if history_manager is None:
            history_manager = self.history_manager

        # Threshold: keep context_window_size + buffer
        threshold = self._summarize_threshold

        if len(history_manager.history) > threshold:
            # Create background task
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._summarize_history_task(history_manager))
                self._active_summarization_tasks.add(task)
                task.add_done_callback(self._active_summarization_tasks.discard)

                print(
                    f"[AgentLLMClient] Summarization task started (History: {len(history_manager.history)})"
                )
            except RuntimeError:
                # No running loop (shouldn't happen in async context usually)
                pass

    async def _summarize_history_task(self, history_manager):
        """Background task to summarize old history.

        Args:
            history_manager: HistoryManager instance to summarize.
        """
        try:
            # Pop oldest messages
            messages_to_summarize = history_manager.pop_oldest(
                self._summarize_batch_size
            )
            if not messages_to_summarize:
                return

            print(
                f"[AgentLLMClient] Summarizing {len(messages_to_summarize)} messages..."
            )

            # Get current summary
            current_summary = history_manager.summary

            # Generate new summary
            new_summary = await self._generate_summary(
                messages_to_summarize, current_summary
            )

            # Update history manager
            history_manager.update_summary(new_summary)
            print(
                f"[AgentLLMClient] Summary updated. New history length: {len(history_manager.history)}"
            )

        except Exception as e:
            print(f"[AgentLLMClient] Summarization failed: {e}")
            import traceback

            traceback.print_exc()

    async def _generate_summary(
        self, messages: List[Dict[str, Any]], current_summary: str
    ) -> str:
        """Generate summary using the LLM."""

        # Format messages
        conversation_text = ""
        for msg in messages:
            role = "User" if msg["role"] == "user" else "Assistant"
            conversation_text += f"{role}: {msg['content']}\n"

        # Build prompt
        prompt = f"""
Summarize the conversation below while preserving important facts, decisions, and open items.
Keep the summary concise and useful for continuing the conversation later.

Current summary:
{current_summary if current_summary else "None"}

New conversation:
{conversation_text}

Updated summary:
"""
        try:
            summary_agent = Agent(
                name=f"{self.agent.name}Summary",
                instructions=self.agent.instructions,
                model=self.agent.model,
                tools=[],
                model_settings=self.agent.model_settings,
            )
            result = await self._turn_runner.run(summary_agent, prompt)
            return result.final_output

        except Exception as e:
            print(f"[AgentLLMClient] Error generating summary: {e}")
            raise e

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: dict = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        """Async version of generate_response"""
        return await self._generate_async(
            user_input,
            stream_callback=stream_callback,
            steering_callback=steering_callback,
        )

    def clear_history(self):
        """Clear conversation history"""
        self.history_manager.clear()

    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history

        Returns:
            List of conversation messages
        """
        return self.history_manager.get_all()

    def _get_available_tools(self) -> List[str]:
        """Get list of available tool names

        Returns:
            List of tool names
        """
        if not getattr(self, "_native_tools_enabled", True):
            return []
        scenario_chat_context = self._get_scenario_chat_context_sync()
        if not scenario_chat_context:
            return self._tool_registry.get_names()
        return [
            name
            for name in self._tool_registry.get_names()
            if is_scenario_workflow_tool_allowed(name, scenario_chat_context)
        ]

    def generate(self, prompt: str) -> str:
        """Simple synchronous generate method for reasoning mode

        Args:
            prompt: The prompt to generate from

        Returns:
            Generated text
        """
        # Use the existing generate_response method
        return self.generate_response(prompt, stream=False)

    async def generate_async(self, prompt: str) -> str:
        """Simple async generate method for reasoning mode

        Args:
            prompt: The prompt to generate from

        Returns:
            Generated text
        """
        # Use the existing async method
        return await self.generate_response_async(prompt)

    async def cleanup(self):
        """Clean up resources, especially MCP connections and memory manager"""
        # Clean up memory manager
        if self.memory_manager:
            try:
                await self.memory_manager.cleanup()
                print("[AgentLLMClient] Memory manager cleaned up")
            except Exception as e:
                print(f"[AgentLLMClient] Error during memory cleanup: {e}")

    def _register_cleanup(self):
        """Register cleanup handler for process exit"""
        import atexit
        import signal

        def sync_cleanup():
            """Synchronous cleanup wrapper"""
            if not self._cleanup_registered:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.cleanup())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

        # Register cleanup on exit
        atexit.register(sync_cleanup)

        # Also handle signals
        def signal_handler(signum, frame):
            sync_cleanup()
            # Re-raise the signal to let default handler run
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
        except:
            pass  # Ignore errors in signal registration

        self._cleanup_registered = True


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

    elif llm_provider in ["antigravity-cli", "claude-cli", "codex-cli"]:
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

    else:  # openai or default
        print(f"[LLM Factory] OpenAI Agentクライアントを作成")
        return AgentLLMClient(
            api_key=config.get("openai_api_key"),
            model=config.get("llm_model", "gpt-4o"),
            config=config,
        )
