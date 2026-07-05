"""
CLI-based LLM Client (supports Antigravity CLI, Claude Code, Codex CLI)

CLI（Antigravity/Claude/Codex）をLLMバックエンドとして使用するクライアント実装。
AgentLLMClientと互換のインターフェースを提供する。
"""

import asyncio
import concurrent.futures
import inspect
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Generator

from ..config import Config
from ..services.project_context import (
    ProjectContextResolver,
    format_minimal_project_context_for_chat_prompt,
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
from .cli_backends.base import CLIBackendBase
from .prompts import build_unified_instructions
from .agentic_completion import (
    render_messages_for_review,
    run_agentic_completion_loop_sync,
)
from .agent_runtime import (
    OpenAIToolCallRecord,
    build_tool_hint_context_sync,
    compose_tool_hint_user_message,
    guard_tool_execution_claims,
    project_context_read_final_response_check,
    project_context_required_read_tool_names,
)
from .cli_tool_context import build_cli_tool_context
from .context_budget import clip_text
from .unified_turn_runtime import run_cli_tool_call_loop

# Memory management
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..memory.history import HistoryManager

# Reasoning support
from ..reasoning import ReasoningManager

# Unified tool registry and adapters
from ..tools.adapters import CLIAdapter
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
from .runtime_tool_registry import build_runtime_tool_registry
from .tool_policy import (
    looks_like_bare_search_followup_request,
    looks_like_project_management_request,
    reset_current_user_input,
    set_current_user_input,
)
from ..services.user_settings_service import get_user_custom_instructions_sync

logger = logging.getLogger(__name__)

DEFAULT_CLI_TOOL_RESULT_MAX_CHARS = 8000


class CLILLMClient:
    """
    CLI-based LLM client (supports Antigravity/Claude/Codex)

    AgentLLMClientと互換性のあるインターフェースを提供し、
    外部CLIツールを通じて推論・応答生成を行う。
    """

    def __init__(self, config: Optional[Config] = None, cli_backend: Optional[CLIBackendBase] = None):
        if config is None:
            raise ValueError("Config is required for CLILLMClient")

        self.config = config
        self.character_name = config.default_character
        self.model_name = config.get('llm_model', 'cli')

        # CLI backend (Antigravity/Claude/Codex)
        if cli_backend is None:
            from .cli_backends.antigravity import AntigravityCLIBackend
            self.cli_backend = AntigravityCLIBackend()
        else:
            self.cli_backend = cli_backend

        logger.info(f"[CLILLMClient] Using {self.cli_backend.get_provider_name()}")

        # Character configuration
        self.character_config = config.get_character_config(self.character_name)

        # Session context
        self.session_user_id: str = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None

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
        self._memory_enabled = get_config_value('memory', {}).get('enabled', True)

        if self._memory_enabled:
            try:
                memory_config = MemoryConfig()
                memory_config.llm_provider = get_config_value(
                    'llm_provider', memory_config.llm_provider
                )
                memory_config.llm_model = get_config_value(
                    'llm_model', memory_config.llm_model
                )
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

        # MCP: CLIネイティブ委譲（MCPPluginは使用しない）
        # Claude Code: --mcp-config で実行時渡し
        # Antigravity CLI / Codex CLI: 各CLIの設定ファイルで事前設定が必要
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
        self._tool_registry = build_runtime_tool_registry(self.config)
        logger.info(
            f"[CLILLMClient] Initialized: character={self.character_name}, "
            f"backend={self.cli_backend.get_provider_name()}, "
            f"tools={len(self._tool_registry)}"
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

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

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
        image_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Union[str, Generator[str, None, None]]:
        logger.info(f"[CLILLMClient] Generating response for: {user_input[:50]}...")
        stream_callback = kwargs.get("stream_callback")

        try:
            response = self._generate_sync(
                user_input,
                image_data=image_data,
                stream_callback=stream_callback,
            )
        except Exception as e:
            logger.error(f"[CLILLMClient] Error: {e}", exc_info=True)
            personality = self.character_config.get('personality', {}) if self.character_config else {}
            response = personality.get('fallbackReply', 'エラーが発生しました')
            self._emit_stream_event_sync(
                stream_callback,
                "stream_end",
                {"content": response},
            )

        if stream:
            def response_generator():
                yield response
            return response_generator()
        return response

    def _generate_sync(
        self,
        user_input: str,
        image_data: Optional[Dict[str, Any]] = None,
        stream_callback: Any = None,
    ) -> str:
        """Core synchronous generation logic"""
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )
        event_callback = self._make_stream_event_callback(stream_callback)
        image_cleanup = None

        try:
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            self._current_context_bundle = self._build_context_bundle_sync(
                user_input, project_context
            )

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

            # Attach image if provided and backend supports it
            prompt = user_input
            if image_data:
                attachment = self.cli_backend.prepare_image_attachment(image_data, cwd=Path.cwd())
                if attachment:
                    suffix, image_cleanup = attachment
                    prompt = user_input + suffix
                    logger.info(f"[CLILLMClient] 画像添付あり: {image_data.get('name', 'unknown')}")
                else:
                    logger.warning(
                        f"[CLILLMClient] {self.cli_backend.get_provider_name()} は画像入力未対応"
                    )

            # Build system context (instructions + history + tools) separately from user input
            system_context = self._build_system_context(user_input=user_input)
            project_context_read_block = self._run_project_context_read_before_cli(
                user_input,
                event_callback=event_callback,
            )
            if project_context_read_block:
                system_context = f"{system_context}\n\n{project_context_read_block}"
            tool_hint_context = build_tool_hint_context_sync(
                user_input=user_input,
                registry=self._tool_registry,
                policy=get_client_generation_policy(self),
                log_prefix="CLILLMClient",
            )
            prompt = compose_tool_hint_user_message(
                prompt,
                tool_hint_context,
            )

            # MCP args (CLI-native delegation)
            mcp_args = self.cli_backend.get_mcp_args(self._mcp_servers) if self._mcp_servers else None

            self._emit_stream_event_sync(
                stream_callback,
                "stream_start",
                {"message": "CLI generation started"},
            )
            self._emit_stream_event_sync(
                stream_callback,
                "status_update",
                {
                    "status": "cli_backend_started",
                    "message": f"{self.cli_backend.get_provider_name()} is running",
                },
            )

            # Execute via CLI: system_context → stdin, prompt → -p
            success, cli_output = self.cli_backend.execute_prompt(
                prompt=prompt,
                cwd=Path.cwd(),
                extra_args=mcp_args,
                system_context=system_context,
                event_callback=event_callback,
            )

            if not success:
                logger.error(f"[CLILLMClient] CLI execution failed: {cli_output}")
                self._emit_stream_event_sync(
                    stream_callback,
                    "stream_end",
                    {
                        "content": f"CLI error: {cli_output}",
                        "status": "failed",
                        "message": "CLI execution failed",
                        "error": str(cli_output or ""),
                    },
                )
                return f"エラーが発生しました: {cli_output}"

            turn_result = run_cli_tool_call_loop(
                original_input=user_input,
                initial_output=cli_output,
                registry=self._tool_registry,
                parse_tool_calls=self.cli_backend.parse_tool_calls,
                execute_follow_up=lambda follow_up: self.cli_backend.execute_prompt(
                    follow_up,
                    cwd=Path.cwd(),
                    event_callback=event_callback,
                ),
                build_follow_up_prompt=self._build_follow_up_prompt,
                log_prefix="CLILLMClient",
                max_tool_result_chars=self._cli_tool_result_max_chars(),
                config=self.config,
                user_input=user_input,
                event_callback=event_callback,
                final_response_check=project_context_read_final_response_check(
                    required=(
                        self._requires_cli_project_context_read(user_input)
                        and not bool(project_context_read_block)
                    ),
                ),
            )
            tool_call_records: list[OpenAIToolCallRecord] = [
                OpenAIToolCallRecord(
                    tool=tool_result.call.tool,
                    arguments=dict(tool_result.call.arguments),
                    result=tool_result.model_output,
                )
                for tool_result in turn_result.tool_results
            ]
            response = turn_result.final_output
            response = guard_tool_execution_claims(response, tool_call_records)
            response = run_agentic_completion_loop_sync(
                client=self,
                run_once=lambda review_prompt: self._run_agentic_review_once(
                    review_prompt,
                    system_context=system_context,
                    event_callback=event_callback,
                    user_input=user_input,
                ),
                context=render_messages_for_review(
                    [
                        {"role": "system", "content": system_context},
                        {"role": "user", "content": prompt},
                    ]
                ),
                user_input=user_input,
                initial_response=response,
            )
            self._emit_stream_event_sync(
                stream_callback,
                "stream_end",
                {"content": response},
            )

            # Update history
            self.history_manager.add_message("user", user_input)
            self.history_manager.add_message("assistant", response)
            self.check_and_summarize_history()

            # Save to memory (async operations run in a new loop)
            self._save_to_memory(user_input, response)

            logger.info(f"[CLILLMClient] Response generated: {len(response)} chars")
            return response
        finally:
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            self._current_context_bundle = None
            if image_cleanup is not None:
                image_cleanup()

    def _run_streamed_with_callback(self, *args, **kwargs):
        """Capability marker: CLI clients emit progress through stream_callback."""
        raise NotImplementedError(
            "CLILLMClient uses generate_response(..., stream_callback=...)"
        )

    def _run_agentic_review_once(
        self,
        prompt: str,
        *,
        system_context: str,
        event_callback: Any,
        user_input: str,
    ) -> str:
        success, cli_output = self.cli_backend.execute_prompt(
            prompt=prompt,
            cwd=Path.cwd(),
            system_context=system_context,
            event_callback=event_callback,
        )
        if not success:
            logger.error(f"[CLILLMClient] agentic review CLI execution failed: {cli_output}")
            return f"エラーが発生しました: {cli_output}"

        turn_result = run_cli_tool_call_loop(
            original_input=user_input,
            initial_output=cli_output,
            registry=self._tool_registry,
            parse_tool_calls=self.cli_backend.parse_tool_calls,
            execute_follow_up=lambda follow_up: self.cli_backend.execute_prompt(
                follow_up,
                cwd=Path.cwd(),
                event_callback=event_callback,
            ),
            build_follow_up_prompt=self._build_follow_up_prompt,
            log_prefix="CLILLMClient",
            max_tool_result_chars=self._cli_tool_result_max_chars(),
            config=self.config,
            user_input=user_input,
            event_callback=event_callback,
            final_response_check=project_context_read_final_response_check(
                required=self._requires_cli_project_context_read(user_input),
            ),
        )
        tool_call_records: list[OpenAIToolCallRecord] = [
            OpenAIToolCallRecord(
                tool=tool_result.call.tool,
                arguments=dict(tool_result.call.arguments),
                result=tool_result.model_output,
            )
            for tool_result in turn_result.tool_results
        ]
        return guard_tool_execution_claims(turn_result.final_output, tool_call_records)

    def _cli_tool_result_max_chars(self) -> int:
        configured = None
        if hasattr(self.config, "get"):
            configured = self.config.get("cli.tool_result_max_chars", None)
            if configured is None:
                configured = self.config.get("llm_cli.tool_result_max_chars", None)
        try:
            value = int(configured) if configured is not None else DEFAULT_CLI_TOOL_RESULT_MAX_CHARS
        except (TypeError, ValueError):
            value = DEFAULT_CLI_TOOL_RESULT_MAX_CHARS
        return max(1000, value)

    def _should_include_cli_project_context(self, user_input: str | None) -> bool:
        if looks_like_bare_search_followup_request(user_input or ""):
            return False
        if getattr(self, "current_include_project_context", False) is True:
            return True
        return looks_like_project_management_request(user_input or "")

    def _requires_cli_project_context_read(self, user_input: str | None) -> bool:
        return (
            getattr(self, "current_include_project_context", None) is True
            and not looks_like_bare_search_followup_request(user_input or "")
            and bool(self.current_project_id or self.current_session_id)
        )

    def _run_project_context_read_before_cli(
        self,
        user_input: str | None,
        *,
        event_callback: Any,
    ) -> str:
        if not self._requires_cli_project_context_read(user_input):
            return ""

        for tool_name in project_context_required_read_tool_names(self._tool_registry):
            args: dict[str, Any] = {}
            if tool_name == "list_project_information" and self.current_project_id:
                args["project_id"] = str(self.current_project_id)

            self._emit_cli_tool_event(
                event_callback,
                "tool_start",
                tool_name=tool_name,
                args=args,
                message=f"Running {tool_name}",
            )
            try:
                result = self._tool_registry.execute(tool_name, **args)
            except Exception as exc:
                self._emit_cli_tool_event(
                    event_callback,
                    "tool_end",
                    tool_name=tool_name,
                    args=args,
                    message=f"Failed {tool_name}",
                    output="",
                    error=str(exc),
                )
                logger.warning(
                    "[CLILLMClient] project context pre-read failed: %s: %s",
                    tool_name,
                    exc,
                )
                continue

            output = str(result)
            self._emit_cli_tool_event(
                event_callback,
                "tool_end",
                tool_name=tool_name,
                args=args,
                message=f"Completed {tool_name}",
                output=output,
                error="",
            )
            clipped = clip_text(output, self._cli_tool_result_max_chars())
            return (
                "## Project Context DB Read Result\n"
                f"- `{tool_name}` was executed before the model response because "
                "Project context is enabled.\n"
                "- Treat this tool result as grounding evidence. Decide normally "
                "whether additional reads or project DB mutation tools are needed.\n\n"
                f"```text\n{clipped}\n```"
            )

        return ""

    def _emit_cli_tool_event(
        self,
        event_callback: Any,
        event_type: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        message: str,
        output: str | None = None,
        error: str = "",
    ) -> None:
        if not event_callback:
            return
        payload: dict[str, Any] = {
            "tool": tool_name,
            "tool_args": dict(args),
            "message": message,
        }
        if event_type == "tool_end":
            payload["tool_result"] = {
                "tool": tool_name,
                "arguments": dict(args),
                "output": output or "",
                "error": error,
            }
        try:
            event_callback(event_type, payload)
        except Exception:
            logger.debug(
                "[CLILLMClient] project context pre-read event callback failed",
                exc_info=True,
            )

    def _run_reasoning(self, user_input: str) -> str:
        """Run reasoning mode synchronously"""
        context = {
            'available_tools': self._get_available_tools(),
            'conversation_history': self.history_manager.get_all(),
            'character_name': self.character_name,
            'project_context': sanitize_project_context_for_chat(
                get_runtime_project_context()
            ) if self._should_include_cli_project_context(user_input) else None,
            'runtime_context': (
                self._current_context_bundle.render_for_prompt()
                if self._current_context_bundle
                else ""
            ),
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._run_async_in_new_loop,
                                     self.reasoning_manager.execute_reasoning_mode(
                                         user_input=user_input,
                                         context=context,
                                     ))
            return future.result()

    def _run_async_in_new_loop(self, coro):
        """Run a coroutine in a new event loop (thread-safe)"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def _make_stream_event_callback(self, stream_callback: Any):
        if stream_callback is None:
            return None

        def _callback(event_type: str, data: dict[str, Any]) -> None:
            self._emit_stream_event_sync(stream_callback, event_type, data)

        return _callback

    def _emit_stream_event_sync(
        self,
        stream_callback: Any,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if stream_callback is None:
            return
        try:
            result = stream_callback(event_type, data)
            if inspect.isawaitable(result):
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    running_loop.create_task(result)
        except Exception:
            logger.debug(
                "[CLILLMClient] Stream callback failed for %s",
                event_type,
                exc_info=True,
            )

    def _save_to_memory(self, user_input: str, response: str):
        """Save conversation to memory manager (runs async in background)"""
        if getattr(self, "external_persistence_enabled", False):
            return
        if not self.memory_manager:
            return

        try:
            user_id = self.session_user_id
            character_name = self.character_name

            async def _save():
                if not self.memory_manager.is_initialized():
                    await self.memory_manager.initialize()
                if self.current_session_id:
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="user",
                        content=user_input,
                        metadata=self.session_metadata.copy(),
                        branch_from_message_id=self.current_edit_message_id,
                    )
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="assistant",
                        content=response,
                    )
                else:
                    await self.memory_manager.add_message(
                        user_id=user_id,
                        character_name=character_name,
                        role="user",
                        content=user_input,
                        metadata=self.session_metadata.copy(),
                        llm_client=self,
                    )
                    await self.memory_manager.add_message(
                        user_id=user_id,
                        character_name=character_name,
                        role="assistant",
                        content=response,
                        llm_client=self,
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
        """Generate a synchronous response for reasoning workflows."""
        return self.generate_response(prompt, stream=False)

    async def generate_async(self, prompt: str) -> str:
        """Generate an async response for reasoning workflows."""
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
        return bool(self._mcp_servers) and (
            bool(self.cli_backend.get_mcp_args(self._mcp_servers))
        )

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

    def _build_system_context(self, user_input: str | None = None) -> str:
        """Build system context for stdin (instructions + history + tools)

        システムプロンプト・会話履歴・ツール情報をまとめて返す。
        各CLI backendが対応する入力形式へ変換して渡す。
        """
        parts = []
        scenario_chat_context = self._get_scenario_chat_context_sync()

        # System instructions (セクションヘッダなし — Geminiが誤解しないように)
        if scenario_chat_context:
            parts.append(scenario_chat_context.prompt)
        elif self.custom_system_prompt:
            parts.append(self.custom_system_prompt)
        else:
            instructions = build_unified_instructions(
                character_name=self.character_name,
                config=self.config,
                include_mcp_info=False,
                custom_instructions=get_user_custom_instructions_sync(
                    self.session_user_id
                ),
            )
            parts.append(instructions)

        context_builder_block = (
            self._current_context_bundle.render_for_prompt()
            if not scenario_chat_context and self._current_context_bundle
            else ""
        )
        if context_builder_block:
            parts.append(f"\n{context_builder_block}")

        include_project_context = self._should_include_cli_project_context(user_input)
        project_context = (
            None
            if scenario_chat_context or not include_project_context
            else get_runtime_project_context()
        )
        if project_context and not context_builder_block:
            parts.append(
                f"\n{format_minimal_project_context_for_chat_prompt(project_context)}"
            )

        # Conversation history
        context_text = self.history_manager.get_context_as_text()
        if context_text:
            parts.append(f"\n会話履歴:\n{context_text}")

        if scenario_chat_context:
            scenario_tools = [
                tool
                for tool in self._tool_registry.get_all()
                if is_scenario_workflow_tool_allowed(
                    tool.name,
                    scenario_chat_context,
                )
            ]
            if scenario_tools:
                tool_prompt = CLIAdapter.to_prompt_text(scenario_tools)
                parts.append(f"\nAvailable scenario tools:\n{tool_prompt}")
            parts.append(
                "\n---\n"
                "Respond according to the dedicated scenario workflow instructions above."
            )
            return "\n".join(parts)

        # Tool information
        tool_prompt = build_cli_tool_context(
            user_input=user_input,
            registry=self._tool_registry,
            force_project_tools=include_project_context,
        )
        if tool_prompt:
            parts.append(f"\n利用可能なツール:\n{tool_prompt}")

        # Final response instruction for the current user utterance.
        parts.append(
            "\n---\n"
            "以下のユーザー発話に、キャラクターとして直接答えてください。"
        )

        parts.append(self._build_tool_execution_contract_prompt())
        return "\n".join(parts)

    def _build_tool_execution_contract_prompt(self) -> str:
        return "\n".join(
            [
                "\nツール実行形式:",
                "- ツールが必要な場合は通常回答ではなく `[TOOL_CALL: tool_name(key=value)]` 形式で出力してください。",
                "- 引数が不要な場合は `[TOOL_CALL: tool_name()]` 形式で出力してください。",
                "- 外部状態を確認・変更した事実は、ツール結果だけを根拠にしてください。",
                "- 追加確認が必要な場合は、最終回答に進まず次のツール呼び出しを出力してください。",
            ]
        )

    def _build_prompt_with_tools(self, user_input: str) -> str:
        """Build combined prompt (fallback for non-stdin backends)"""
        system_context = self._build_system_context(user_input=user_input)
        return f"{system_context}\n\nUser: {user_input}\nAssistant:"

    def _build_follow_up_prompt(
        self, original_input: str, initial_response: str, tool_results_text: str
    ) -> str:
        parts = [
            "# ツール実行結果",
        ]
        scenario_chat_context = self._get_scenario_chat_context_sync()
        if scenario_chat_context:
            parts.extend([scenario_chat_context.prompt, ""])
        task_instruction = (
            "上記のツール結果に基づいて、シナリオ進行の応答を続けてください。"
            if scenario_chat_context
            else "上記のツール結果に基づいて、ユーザーに自然に回答してください。"
        )
        final_reminder = (
            "専用シナリオ進行指示に従ってください。"
            if scenario_chat_context
            else f"キャラクターとしての応答を維持してください: {self.character_name}"
        )
        parts.extend([
            "ツール結果を受け取った後は、まず元のユーザー要求がすでに満たされたかを判定してください。",
            "満たされている場合は、追加の `[TOOL_CALL: ...]` を出さず、直ちに最終回答してください。",
            "追加の `[TOOL_CALL: tool_name(key=value)]` が許されるのは、直前の結果がエラー、空、不完全、不整合、古い、元の要求に未完了の別サブタスクが残っている、直前の結果で新しい確認対象が発生した、または変更系作業で変更後の検証が未完了の場合だけです。",
            "「念のため」「もう一度確認」だけを理由に、成功したツール結果を再確認しないでください。",
            "同じツールの再実行は、変更後検証、変更されたファイルの確認、エラー解消、未完了サブタスクの処理など、具体的な新しい理由がある場合だけ許可されます。",
            "",
            "元のユーザー発話:",
            original_input,
            "",
            "直前の出力:",
            initial_response,
            "",
            "ツール結果:",
            tool_results_text,
            "",
            "# 最終回答",
            task_instruction,
            final_reminder,
            "\nAssistant:",
        ])
        return "\n".join(parts)

    def _build_context_bundle_sync(
        self, user_input: str, project_context: Optional[dict[str, Any]]
    ) -> Optional[ContextBundle]:
        if self._get_scenario_chat_context_sync():
            return None
        include_project_context = self._should_include_cli_project_context(user_input)
        try:
            return self._run_async_in_new_loop(
                ContextBuilder().build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id if include_project_context else None,
                    session_id=self.current_session_id,
                    project_context=project_context if include_project_context else None,
                    include_project_context=include_project_context,
                    include_project_information=False,
                    include_project_pack=False,
                    include_task_context=False,
                    project_context_mode="minimal",
                )
            )
        except Exception as e:
            logger.warning(
                f"[CLILLMClient] ContextBuilder failed; fallback to basic context: {e}"
            )
            return None

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        if not self.current_project_id and not self.current_session_id:
            return None

        if self._get_scenario_chat_context_sync():
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_in_new_loop(
                resolver.resolve_context(
                    project_id=self.current_project_id,
                    session_id=self.current_session_id,
                )
            )
        except Exception as e:
            logger.warning(f"[CLILLMClient] Failed to resolve project context: {e}")
            return None

    def _get_scenario_chat_context_sync(self):
        if not self.current_session_id:
            return None
        try:
            return self._run_async_in_new_loop(
                build_scenario_chat_context(self.current_session_id)
            )
        except Exception as e:
            logger.warning(f"[CLILLMClient] Failed to resolve scenario chat context: {e}")
            return None

    def _get_available_tools(self) -> List[str]:
        scenario_chat_context = self._get_scenario_chat_context_sync()
        if not scenario_chat_context:
            return self._tool_registry.get_names()
        return [
            name
            for name in self._tool_registry.get_names()
            if is_scenario_workflow_tool_allowed(name, scenario_chat_context)
        ]
