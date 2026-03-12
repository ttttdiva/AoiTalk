"""
CLI-based LLM Client (supports Gemini CLI, Claude Code, Codex CLI)

CLI（Gemini/Claude/Codex）をLLMバックエンドとして使用するクライアント実装。
AgentLLMClientと互換のインターフェースを提供する。
"""

import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Generator

from ..config import Config
from .cli_backends.base import CLIBackendBase
from .prompts import build_unified_instructions

# Memory management
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..memory.history import HistoryManager

try:
    from ..memory.semantic_memory import SemanticMemoryManager
    SEMANTIC_MEMORY_AVAILABLE = True
except (ImportError, Exception):
    SEMANTIC_MEMORY_AVAILABLE = False
    SemanticMemoryManager = None

# Reasoning support
from ..reasoning import ReasoningManager

# Unified tool registry and adapters
from ..tools.registry import get_registry
from ..tools.adapters import CLIAdapter

logger = logging.getLogger(__name__)


class CLILLMClient:
    """
    CLI-based LLM client (supports Gemini/Claude/Codex)

    AgentLLMClientと互換性のあるインターフェースを提供し、
    外部CLIツールを通じて推論・応答生成を行う。
    """

    def __init__(self, config: Optional[Config] = None, cli_backend: Optional[CLIBackendBase] = None):
        if config is None:
            raise ValueError("Config is required for CLILLMClient")

        self.config = config
        self.character_name = config.default_character
        self.model_name = config.get('llm_model', 'cli')

        # CLI backend (Gemini/Claude/Codex)
        if cli_backend is None:
            from .cli_backends.gemini import GeminiCLIBackend
            self.cli_backend = GeminiCLIBackend()
        else:
            self.cli_backend = cli_backend

        logger.info(f"[CLILLMClient] Using {self.cli_backend.get_provider_name()}")

        # Character configuration
        self.character_config = config.get_character_config(self.character_name)

        # Session context
        self.session_user_id: str = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None

        # History manager (HistoryManager を使用、独自リストではなく)
        self.history_manager = HistoryManager()
        self._summarize_batch_size = 10
        self._summarize_threshold = 20

        # Custom system prompt
        self.custom_system_prompt: Optional[str] = None

        # LLM mode
        self._current_llm_mode = 'fast'

        # Config helper
        def get_config_value(key, default=None):
            if hasattr(config, 'get'):
                return config.get(key, default)
            return default

        # Memory manager (ConversationMemoryManager)
        self.memory_manager = None
        self.semantic_memory_manager = None
        self._memory_enabled = get_config_value('memory', {}).get('enabled', True)

        if self._memory_enabled:
            try:
                memory_config = MemoryConfig()
                memory_config.llm_provider = get_config_value('llm_provider', 'gemini')
                memory_config.llm_model = get_config_value('llm_model', 'gemini-3-flash-preview')
                memory_settings = get_config_value('memory', {})
                memory_config.embedding_model = memory_settings.get(
                    'embedding_model', memory_config.embedding_model
                )
                memory_config.preload_embedding_model = memory_settings.get(
                    'preload_embedding_model', memory_config.preload_embedding_model
                )
                memory_config.enable_search = memory_settings.get(
                    'enable_search', memory_config.enable_search
                )
                self.memory_manager = ConversationMemoryManager(memory_config, app_config=config)
                logger.info("[CLILLMClient] ConversationMemoryManager初期化完了")
            except Exception as e:
                logger.warning(f"[CLILLMClient] Memory manager initialization failed: {e}")

            # Semantic memory (Mem0)
            enable_search = get_config_value('memory', {}).get('enable_search', False)
            if SEMANTIC_MEMORY_AVAILABLE and SemanticMemoryManager and enable_search:
                try:
                    self.semantic_memory_manager = SemanticMemoryManager(config)
                    logger.info("[CLILLMClient] Semantic memory (Mem0) enabled")
                except Exception as e:
                    logger.warning(f"[CLILLMClient] Semantic memory init failed: {e}")

        # MCP: CLIネイティブ委譲（MCPPluginは使用しない）
        # Claude Code: --mcp-config で実行時渡し
        # Gemini CLI / Codex CLI: 各CLIの設定ファイルで事前設定が必要
        self._mcp_servers: dict = {}
        if config.get('mcp_enabled', False):
            self._mcp_servers = config.get('mcp', {}).get('servers', {})
            if self._mcp_servers:
                mcp_args = self.cli_backend.get_mcp_args(self._mcp_servers)
                if mcp_args:
                    logger.info(
                        f"[CLILLMClient] MCP: {len(self._mcp_servers)} server(s) will be "
                        f"passed via CLI args ({self.cli_backend.get_provider_name()})"
                    )
                else:
                    logger.info(
                        f"[CLILLMClient] MCP: {len(self._mcp_servers)} server(s) in config, "
                        f"but {self.cli_backend.get_provider_name()} requires native settings file"
                    )

        # Reasoning manager
        self.reasoning_manager = None
        reasoning_config = get_config_value('reasoning', {})
        if reasoning_config.get('enabled', False):
            self.reasoning_manager = ReasoningManager(self, reasoning_config)
            logger.info(
                f"[CLILLMClient] 推論モード初期化完了 "
                f"(閾値: {reasoning_config.get('complexity_threshold', 0.6)})"
            )

        # Spotify agent (CLI backend では無効)
        self.spotify_agent = None

        # Tool registry
        self._unified_registry = get_registry()
        logger.info(
            f"[CLILLMClient] Initialized: character={self.character_name}, "
            f"backend={self.cli_backend.get_provider_name()}, "
            f"tools={len(self._unified_registry)}"
        )

    # ------------------------------------------------------------------
    # Session / Character management
    # ------------------------------------------------------------------

    def set_session_context(
        self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None
    ):
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}

    def set_character(self, character_name: str):
        self.character_name = character_name
        self.character_config = self.config.get_character_config(character_name)
        logger.info(f"[CLILLMClient] Character changed to: {character_name}")

    def update_character(self, yaml_filename: str):
        if self.config:
            new_config = self.config.get_character_config(yaml_filename)
            if new_config:
                self.character_name = new_config.get('name', yaml_filename)
                self.character_config = new_config
                self.clear_history()
                logger.info(f"[CLILLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)")
            else:
                logger.warning(f"[CLILLMClient] キャラクター設定が見つかりません: {yaml_filename}")

    def set_system_prompt(self, prompt: str):
        self.custom_system_prompt = prompt
        logger.info(f"[CLILLMClient] Custom system prompt set: {prompt[:50]}...")

    def set_llm_mode(self, mode: str):
        if mode not in ['fast', 'thinking']:
            logger.warning(f"[CLILLMClient] Invalid mode '{mode}', defaulting to 'fast'")
            mode = 'fast'
        self._current_llm_mode = mode
        logger.info(f"[CLILLMClient] LLM mode set to: {mode}")

    def get_llm_mode(self) -> str:
        return self._current_llm_mode

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self):
        self.history_manager.clear()
        logger.info("[CLILLMClient] Conversation history cleared")

    def get_history(self) -> List[Dict[str, Any]]:
        return self.history_manager.get_all()

    def check_and_summarize_history(self, history_manager=None) -> None:
        hm = history_manager or self.history_manager
        if len(hm.history) > self._summarize_threshold:
            hm.pop_oldest(self._summarize_batch_size)
            logger.info(f"[CLILLMClient] History truncated to {len(hm.history)} messages")

    # ------------------------------------------------------------------
    # Response generation (sync)
    # ------------------------------------------------------------------

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs,
    ) -> Union[str, Generator[str, None, None]]:
        logger.info(f"[CLILLMClient] Generating response for: {user_input[:50]}...")

        try:
            response = self._generate_sync(user_input)
        except Exception as e:
            logger.error(f"[CLILLMClient] Error: {e}", exc_info=True)
            personality = self.character_config.get('personality', {}) if self.character_config else {}
            response = personality.get('fallbackReply', 'エラーが発生しました')

        if stream:
            def response_generator():
                yield response
            return response_generator()
        return response

    def _generate_sync(self, user_input: str) -> str:
        """Core synchronous generation logic"""

        # Reasoning mode check
        if self.reasoning_manager:
            try:
                if self.reasoning_manager.is_reasoning_required(
                    user_input, self._get_available_tools()
                ):
                    logger.info("[CLILLMClient] Using reasoning mode")
                    return self._run_reasoning(user_input)
            except Exception as e:
                logger.warning(f"[CLILLMClient] Reasoning check failed: {e}")

        # Build system context (instructions + history + tools) separately from user input
        system_context = self._build_system_context()

        # MCP args (CLI-native delegation)
        mcp_args = self.cli_backend.get_mcp_args(self._mcp_servers) if self._mcp_servers else None

        # Execute via CLI: system_context → stdin, user_input → -p
        success, cli_output = self.cli_backend.execute_prompt(
            prompt=user_input,
            cwd=Path.cwd(),
            extra_args=mcp_args,
            system_context=system_context,
        )

        if not success:
            logger.error(f"[CLILLMClient] CLI execution failed: {cli_output}")
            return f"エラーが発生しました: {cli_output}"

        # Parse and execute tool calls
        tool_calls = self.cli_backend.parse_tool_calls(cli_output)
        if tool_calls:
            logger.info(f"[CLILLMClient] Executing {len(tool_calls)} tool call(s)")
            tool_results = CLIAdapter.execute_tool_calls(tool_calls, get_registry())
            results_text = CLIAdapter.format_tool_results(tool_results)

            follow_up = self._build_follow_up_prompt(user_input, cli_output, results_text)
            success2, final_output = self.cli_backend.execute_prompt(follow_up, cwd=Path.cwd())
            response = final_output if success2 else cli_output
        else:
            response = cli_output

        # Update history
        self.history_manager.add_message("user", user_input)
        self.history_manager.add_message("assistant", response)
        self.check_and_summarize_history()

        # Save to memory (async operations run in a new loop)
        self._save_to_memory(user_input, response)

        logger.info(f"[CLILLMClient] Response generated: {len(response)} chars")
        return response

    def _run_reasoning(self, user_input: str) -> str:
        """Run reasoning mode synchronously"""
        context = {
            'available_tools': self._get_available_tools(),
            'conversation_history': self.history_manager.get_all(),
            'character_name': self.character_name,
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._run_async_in_new_loop,
                                     self.reasoning_manager.execute_reasoning_mode(
                                         user_input=user_input,
                                         context=context,
                                     ))
            return future.result(timeout=300)

    def _run_async_in_new_loop(self, coro):
        """Run a coroutine in a new event loop (thread-safe)"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _save_to_memory(self, user_input: str, response: str):
        """Save conversation to memory manager (runs async in background)"""
        if not self.memory_manager:
            return

        try:
            user_id = self.session_user_id
            character_name = self.character_name

            async def _save():
                if not self.memory_manager.is_initialized():
                    await self.memory_manager.initialize()
                await self.memory_manager.add_message(
                    user_id=user_id,
                    character_name=character_name,
                    role="user",
                    content=user_input,
                    metadata=self.session_metadata.copy(),
                )
                await self.memory_manager.add_message(
                    user_id=user_id,
                    character_name=character_name,
                    role="assistant",
                    content=response,
                )

            self._run_async_in_new_loop(_save())
        except Exception as e:
            logger.warning(f"[CLILLMClient] Failed to save memory: {e}")

    # ------------------------------------------------------------------
    # Response generation (async)
    # ------------------------------------------------------------------

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        return await asyncio.to_thread(
            self.generate_response, user_input, temperature, max_tokens, stream=False, **kwargs
        )

    def generate(self, prompt: str) -> str:
        """Simple synchronous generate (reasoning mode compatibility)"""
        return self.generate_response(prompt, stream=False)

    async def generate_async(self, prompt: str) -> str:
        """Simple async generate (reasoning mode compatibility)"""
        return await self.generate_response_async(prompt)

    # ------------------------------------------------------------------
    # MCP (CLIネイティブ委譲)
    # ------------------------------------------------------------------

    def is_mcp_available(self) -> bool:
        """MCP availability check

        CLI backends delegate MCP to the CLI tool itself.
        Returns True if MCP servers are configured and the backend supports
        runtime MCP args (currently only Claude Code).
        """
        return bool(self._mcp_servers) and bool(self.cli_backend.get_mcp_args(self._mcp_servers))

    async def add_mcp_server(
        self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None
    ) -> bool:
        """Add MCP server (interface compatibility stub)

        CLI backends manage MCP natively. This method is kept for interface
        compatibility but only updates the internal config dict.
        """
        logger.info(
            f"[CLILLMClient] add_mcp_server called for '{name}' — "
            f"CLI backends manage MCP natively"
        )
        return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self):
        if self.memory_manager:
            try:
                await self.memory_manager.cleanup()
                logger.info("[CLILLMClient] Memory manager cleaned up")
            except Exception as e:
                logger.warning(f"[CLILLMClient] Memory cleanup error: {e}")

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_system_context(self) -> str:
        """Build system context for stdin (instructions + history + tools)

        システムプロンプト・会話履歴・ツール情報をまとめて返す。
        Gemini CLIではstdinで渡し、ユーザーメッセージは-pで別途渡す。
        """
        parts = []

        # System instructions (セクションヘッダなし — Geminiが誤解しないように)
        if self.custom_system_prompt:
            parts.append(self.custom_system_prompt)
        else:
            instructions = build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                include_mcp_info=False,
            )
            parts.append(instructions)

        # Conversation history
        context_text = self.history_manager.get_context_as_text()
        if context_text:
            parts.append(f"\n会話履歴:\n{context_text}")

        # Tool information
        registry = get_registry()
        all_tools = registry.get_all()
        if all_tools:
            tool_prompt = CLIAdapter.to_prompt_text(all_tools)
            parts.append(f"\n利用可能なツール:\n{tool_prompt}")

        # 明示的な応答指示（設定確認の応答を防止）
        parts.append(
            "\n---\n"
            "上記の設定に従い、以下のユーザーの発言にキャラクターとして直接応答してください。"
            "設定の確認や読み込みの報告は不要です。"
        )

        return "\n".join(parts)

    def _build_prompt_with_tools(self, user_input: str) -> str:
        """Build combined prompt (fallback for non-stdin backends)"""
        system_context = self._build_system_context()
        return f"{system_context}\n\nUser: {user_input}\nAssistant:"

    def _build_follow_up_prompt(
        self, original_input: str, initial_response: str, tool_results_text: str
    ) -> str:
        parts = [
            "# Tool Execution Results",
            f"Original User Input: {original_input}",
            f"\nYour Initial Response: {initial_response}",
            f"\n{tool_results_text}",
            "\n# Your Task",
            "Based on the tool results above, provide a natural, helpful response to the user.",
            f"Remember to stay in character as {self.character_name}.",
            "\nAssistant:",
        ]
        return "\n".join(parts)

    def _get_available_tools(self) -> List[str]:
        return get_registry().get_names()


# Backward compatibility alias
GeminiCLIBackend = CLILLMClient
