"""
Character-preserving LLM client using OpenAI Agents SDK with tools or Gemini
"""
import asyncio
import concurrent.futures
import os
import logging
from typing import Optional, List, Dict, Any, Union, Generator
from agents import Agent, Runner, WebSearchTool

from ..config import Config
from .gemini_engine import GeminiLLMClient
from .gemini_agent_engine import GeminiAgentLLMClient
from ..tools import (
    create_mcp_tool_wrapper,
    init_spotify_manager,
)
from ..tools.registry import get_registry
from ..tools.adapters import OpenAIAgentAdapter
from ..tools.core import ToolDefinition
from ..tools.external.mcp_plugin import MCPPlugin
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
# Conditionally import semantic memory to avoid SQLite issues
try:
    from ..memory.semantic_memory import SemanticMemoryManager
    SEMANTIC_MEMORY_AVAILABLE = True
except (ImportError, Exception) as e:
    print(f"[AgentLLMClient] Semantic memory not available: {e}")
    SEMANTIC_MEMORY_AVAILABLE = False
    SemanticMemoryManager = None
from ..agents import SpotifyAgent
from .prompts import build_unified_instructions
from ..reasoning import ReasoningManager


class AgentLLMClient:
    """Character-based LLM client using OpenAI Agents SDK with tools"""
    
    def __init__(self, api_key: str, model: str = "gpt-4o", use_agent: bool = True, config: Optional[Config] = None):
        """Initialize tool-based LLM client
        
        Args:
            api_key: OpenAI API key
            model: Model to use (kept for compatibility)
            use_agent: Whether to use agent mode (kept for compatibility)
            config: Application configuration (required)
        """
        self.config = config
        self.model_name = model
        # Handle both Config object and dict
        if hasattr(config, 'default_character'):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get('default_character', "Assistant")
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
        self.current_session_id: Optional[str] = None  # For session-specific message storage
        
        # Initialize memory manager
        self.memory_manager = None
        self.semantic_memory_manager = None
        # Handle config access consistently
        def get_config_value(key, default=None):
            if hasattr(config, 'get'):
                return config.get(key, default)
            elif isinstance(config, dict):
                return config.get(key, default)
            else:
                return default
        
        self._memory_enabled = get_config_value('memory', {}).get('enabled', True)
        if self._memory_enabled:
            memory_config = MemoryConfig()
            if config:
                # Override with app config if available
                memory_config.llm_provider = get_config_value('llm_provider', 'gemini')
                memory_config.llm_model = get_config_value('llm_model', 'gemini-3-flash-preview')
                # Override embedding model configuration
                memory_settings = get_config_value('memory', {})
                memory_config.embedding_model = memory_settings.get('embedding_model', memory_config.embedding_model)
                memory_config.preload_embedding_model = memory_settings.get('preload_embedding_model', memory_config.preload_embedding_model)
                # Override enable_search setting
                memory_config.enable_search = memory_settings.get('enable_search', memory_config.enable_search)
                print(f"[AgentLLMClient] Memory config - enable_search: {memory_config.enable_search} (from config: {memory_settings.get('enable_search', 'not set')})")
            self.memory_manager = ConversationMemoryManager(memory_config, app_config=config)
            
            # Initialize semantic memory (Mem0) only if search is enabled
            memory_settings = get_config_value('memory', {})
            enable_search = memory_settings.get('enable_search', False)
            
            if SEMANTIC_MEMORY_AVAILABLE and SemanticMemoryManager and enable_search:
                try:
                    self.semantic_memory_manager = SemanticMemoryManager(config)
                    print(f"[AgentLLMClient] Semantic memory (Mem0) enabled for search")
                except Exception as e:
                    print(f"[AgentLLMClient] Failed to initialize semantic memory: {e}")
                    self.semantic_memory_manager = None
            else:
                self.semantic_memory_manager = None
                if not enable_search:
                    print(f"[AgentLLMClient] Semantic memory disabled (enable_search: false)")
        
        # Initialize MCP plugin
        self.mcp_plugin = MCPPlugin()
        self._mcp_initialized = False
        self.available_mcp_servers = {}  # Will be updated when MCP is initialized
        self._cleanup_registered = False  # Track if cleanup is registered
        
        # Skip synchronous MCP initialization - will be done asynchronously on first use
        if self.config and self.config.get('mcp_enabled', False):
            print(f"[AgentLLMClient] MCPは有効です（初回使用時に初期化）")
        else:
            print(f"[AgentLLMClient] MCPは無効です")
        
        # Initialize Spotify agent (conditional)
        self.spotify_agent = None
        spotify_enabled = get_config_value('agents', {}).get('spotify', {}).get('enabled', True)
        if spotify_enabled:
            self.spotify_agent = SpotifyAgent(model="gpt-4o-mini")
            print(f"[AgentLLMClient] Spotify Agent初期化完了")
        else:
            print(f"[AgentLLMClient] Spotify Agentは無効です")
        
        # Initialize reasoning manager
        self.reasoning_manager = None
        if self.config:
            reasoning_config = get_config_value('reasoning', {})
            if reasoning_config.get('enabled', False):
                self.reasoning_manager = ReasoningManager(self, reasoning_config)
                print(f"[AgentLLMClient] 推論モード初期化完了 (閾値: {reasoning_config.get('complexity_threshold', 0.6)})")
        
        self.agent = self._create_character_agent()
        print(f"[AgentLLMClient] キャラクターエージェント初期化: {self.character_name}")
        
        
        # Initialize Spotify
        spotify_enabled = get_config_value('spotify', {}).get('enabled', True)
        if self.config and spotify_enabled:
            spotify_success = init_spotify_manager()
            if spotify_success:
                print(f"[AgentLLMClient] Spotify初期化成功")
            else:
                print(f"[AgentLLMClient] Spotify初期化スキップ（設定不完全）")
        elif not spotify_enabled:
            print(f"[AgentLLMClient] Spotify機能は無効化されています")
            
        # Register cleanup handler
        self._register_cleanup()
    
    def _initialize_mcp_sync(self):
        """Initialize MCP plugin synchronously"""
        import asyncio
        
        # Check if MCP is enabled in configuration
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[AgentLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[AgentLLMClient] MCP SDK not available")
            return
        
        try:
            # Try to use existing event loop if possible
            try:
                loop = asyncio.get_running_loop()
                # If we're in an event loop, defer initialization
                print("[AgentLLMClient] Event loop detected, deferring MCP initialization")
                return
            except RuntimeError:
                # No running loop, we can create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    result = loop.run_until_complete(self._do_mcp_init())
                    if result:
                        print("[AgentLLMClient] MCP synchronous initialization completed")
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)
                    
        except Exception as e:
            print(f"[AgentLLMClient] MCP initialization error: {e}")
            import traceback
            traceback.print_exc()

    async def _do_mcp_init(self):
        """Actual MCP initialization logic"""
        try:
            # Get MCP configuration from config
            mcp_config = self.config.get('mcp', {}) if self.config else {}
            # Add timeout for MCP initialization
            success = await asyncio.wait_for(
                self.mcp_plugin.initialize(mcp_config),
                timeout=60.0  # 60 second timeout for full MCP initialization
            )
            if success:
                self._mcp_initialized = True
                print("[AgentLLMClient] MCP plugin initialized successfully")
                
                # Log available MCP tools
                tools = await self.mcp_plugin.get_tools_for_agent()
                if tools:
                    print(f"[AgentLLMClient] MCP tools available: {len(tools)}")
                    
                    # Create detailed MCP tool instructions
                    mcp_instructions = "\n\nMCPツールで利用可能なサービス:\n"
                    servers_tools = {}
                    
                    for tool in tools:
                        server_name = tool.get('server_name', 'unknown')
                        tool_name = tool.get('tool_name', 'unknown')
                        description = tool['function']['description']
                        
                        if server_name not in servers_tools:
                            servers_tools[server_name] = []
                        servers_tools[server_name].append({
                            'name': tool_name,
                            'description': description
                        })
                        
                        print(f"  - {server_name}.{tool_name}: {description}")
                    
                    # Update available MCP capabilities for dynamic instruction building
                    self.available_mcp_servers = servers_tools
                    print(f"[AgentLLMClient] MCP capabilities detected: {list(servers_tools.keys())}")
                    return True
            else:
                print("[AgentLLMClient] Failed to initialize MCP plugin")
                return False
        except asyncio.TimeoutError:
            print(f"[AgentLLMClient] MCP initialization timed out after 60 seconds")
            return False
        except Exception as e:
            print(f"[AgentLLMClient] Error initializing MCP: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _initialize_mcp(self):
        """Initialize MCP plugin asynchronously (legacy method)"""
        # Check if MCP is enabled in configuration
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[AgentLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[AgentLLMClient] MCP SDK not available")
            return
            
        try:
            # Debug event loop info
            import asyncio
            try:
                current_loop = asyncio.get_running_loop()
                print(f"[AgentLLMClient] MCP init event loop: {id(current_loop)}")
            except RuntimeError:
                print(f"[AgentLLMClient] No running event loop during MCP init")
            
            # Get MCP configuration from config
            mcp_config = self.config.get('mcp', {}) if self.config else {}
            # Add timeout for MCP initialization
            success = await asyncio.wait_for(
                self.mcp_plugin.initialize(mcp_config),
                timeout=60.0  # 60 second timeout for full MCP initialization
            )
            if success:
                self._mcp_initialized = True
                print("[AgentLLMClient] MCP plugin initialized successfully")
                
                # Log available MCP tools
                tools = await self.mcp_plugin.get_tools_for_agent()
                if tools:
                    print(f"[AgentLLMClient] MCP tools available: {len(tools)}")
                    
                    # Create detailed MCP tool instructions
                    mcp_instructions = "\n\nMCPツールで利用可能なサービス:\n"
                    servers_tools = {}
                    
                    for tool in tools:
                        server_name = tool.get('server_name', 'unknown')
                        tool_name = tool.get('tool_name', 'unknown')
                        description = tool['function']['description']
                        
                        if server_name not in servers_tools:
                            servers_tools[server_name] = []
                        servers_tools[server_name].append({
                            'name': tool_name,
                            'description': description
                        })
                        
                        print(f"  - {server_name}.{tool_name}: {description}")
                    
                    # Update available MCP capabilities for dynamic instruction building
                    self.available_mcp_servers = servers_tools
                    print(f"[AgentLLMClient] MCP capabilities detected: {list(servers_tools.keys())}")
            else:
                print("[AgentLLMClient] Failed to initialize MCP plugin")
        except asyncio.TimeoutError:
            print(f"[AgentLLMClient] MCP initialization timed out after 60 seconds")
        except Exception as e:
            print(f"[AgentLLMClient] Error initializing MCP: {e}")
            import traceback
            traceback.print_exc()
    
    
    async def _update_agent_with_mcp_tools(self):
        """Update agent with MCP tools by recreating it"""
        if not self._mcp_initialized:
            return

        # 統一レジストリからツールを取得し、FunctionToolに変換
        self.agent = self._create_character_agent()

        print(f"[AgentLLMClient] エージェントをMCPツールと共に更新: MCP機能追加")
    
    def _add_specialist_agents_to_tools(self, base_tools: list) -> None:
        """専門エージェントをツールリストに追加する共通メソッド
        
        Args:
            base_tools: ツールリスト（参照渡しで直接変更）
        """
        # Add Spotify agent if enabled
        if self.spotify_agent:
            print(f"[AgentLLMClient] Adding Spotify agent as tool...")
            sp_tool = self.spotify_agent.as_tool()
            base_tools.append(sp_tool)
            print(f"[AgentLLMClient] Spotify tool added: {getattr(sp_tool, 'name', 'unknown')}")
        
        # ClickUpはMCP経由に移行済み
    
    def _build_instructions(self, include_mcp_info: bool = False) -> str:
        """統一的なシステムプロンプトを生成（共通関数を使用）
        
        Args:
            include_mcp_info: MCP情報を含むかどうか
            
        Returns:
            システムプロンプト文字列
        """
        return build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            include_mcp_info=include_mcp_info,
            available_mcp_servers=self.available_mcp_servers if self._mcp_initialized else None
        )
    
    def _create_character_agent(self) -> Agent:
        """Create character agent with tools from unified registry + MCP"""
        from agents import WebSearchTool

        # 統一レジストリから直接登録ツール（MCP化対象外）を取得し、FunctionToolに変換
        registry = get_registry()
        base_tools = [
            WebSearchTool(),
            *OpenAIAgentAdapter.convert_all(registry.get_all()),
        ]

        # Add specialist agents (Spotify agent etc.)
        self._add_specialist_agents_to_tools(base_tools)

        # キャラクター名を決定
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            character_name = character_config.get('name', self.character_name)
        else:
            character_name = "MainAssistant"

        # MCPサーバーリストを準備（MCP移行済みツールはここから提供される）
        mcp_servers = []
        if self._mcp_initialized and self.mcp_plugin:
            mcp_servers.append(self.mcp_plugin)

        return Agent(
            name=character_name,
            instructions=self._build_instructions(include_mcp_info=self._mcp_initialized),
            tools=base_tools,
            model=self.model_name,
            mcp_servers=mcp_servers
        )
    
    def _build_conversation_context(self) -> str:
        """Build conversation context from history"""
        # HistoryManagerのget_context_as_textを使用するか、既存ロジックを再現
        # ここでは既存ロジックを忠実に再現する
        
        history = self.history_manager.get_all()
        if not history:
            return ""
        
        current_input = history[-1]["content"]
        
        if len(history) == 1:
            return current_input
            
        # Get context window size from manager
        context_window = self.history_manager.context_window_size
        
        # Original logic: history[-11:-1] -> up to 10 items before the last one
        relevant_history = history[-(context_window + 1):-1]
        
        context_parts = []
        for msg in relevant_history:
            if msg["role"] == "user":
                context_parts.append(f"ユーザー: {msg['content']}")
            else:
                context_parts.append(f"アシスタント: {msg['content']}")
        
        if context_parts:
            context = f"過去の会話:\n" + "\n".join(context_parts) + f"\n\n現在の質問: {current_input}"
        else:
            context = f"現在の質問: {current_input}"
            
        return context
    
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
                self.character_name = new_config.get('name', yaml_filename)
                # Clear conversation history when switching characters
                self.clear_history()
                self.agent = self._create_character_agent()
                print(f"[AgentLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)")
            else:
                print(f"[AgentLLMClient] キャラクター設定が見つかりません: {yaml_filename}")
        else:
            print(f"[AgentLLMClient] 設定オブジェクトがありません")
    
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
        
        Note: This is primarily used for SGLang/Qwen3 thinking mode.
              For OpenAI agents, this is stored but has no effect.
        """
        self._current_llm_mode = mode
        print(f"[AgentLLMClient] LLM mode set to: {mode}")
    
    def get_llm_mode(self) -> str:
        """Get current LLM response mode
        
        Returns:
            Current mode ('fast' or 'thinking')
        """
        return getattr(self, '_current_llm_mode', 'fast')


    def generate_response(self, 
                         user_input: str,
                         temperature: float = 0.7,
                         max_tokens: Optional[int] = None,
                         stream: bool = False) -> Union[str, Generator[str, None, None]]:
        """Generate response using OpenAI Agents SDK
        
        Args:
            user_input: User's input text
            temperature: Sampling temperature (kept for compatibility)
            max_tokens: Maximum tokens (kept for compatibility)
            stream: Whether to stream (kept for compatibility)
            
        Returns:
            Generated response
        """
        try:
            # Always use a new event loop in a thread to avoid conflicts
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._run_async_safe, user_input)
                response = future.result(timeout=300)  # 5 minute timeout
            
            print(f"[AgentLLMClient] 応答: {response}")
            
            if stream:
                # Return as generator for compatibility
                def response_generator():
                    yield response
                return response_generator()
            return response
                
        except concurrent.futures.TimeoutError:
            print(f"[AgentLLMClient] タイムアウトエラー")
            personality = self.config.get_character_config(self.character_name).get('personality', {}) if self.config else {}
            return personality.get('fallbackReply', 'タイムアウトエラーが発生しました')
        except Exception as e:
            print(f"[AgentLLMClient] エラー: {e}")
            import traceback
            traceback.print_exc()
            personality = self.config.get_character_config(self.character_name).get('personality', {}) if self.config else {}
            return personality.get('fallbackReply', 'エラーが発生しました')

    def set_session_context(self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
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
    
    def _run_async_safe(self, user_input: str) -> str:
        """Safely run async code in a new event loop"""
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Run the async function
            return loop.run_until_complete(self._generate_async(user_input))
        finally:
            # Close the loop
            loop.close()
    
    async def _generate_async(self, user_input: str) -> str:
        """Generate response asynchronously using character agent with tools"""
        # Ensure MCP is initialized before generating response
        if not self._mcp_initialized and self.config and self.config.get('mcp_enabled', False):
            await self._initialize_mcp()
            # Recreate agent with MCP tools if MCP was just initialized
            if self._mcp_initialized:
                await self._update_agent_with_mcp_tools()
        
        # Initialize memory manager if needed
        if self.memory_manager and not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()
        
        # Add user message to memory
        if self.memory_manager:
            try:
                if self.current_session_id:
                    # Use session-specific storage when session ID is set
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="user",
                        content=user_input,
                        metadata=self._get_memory_metadata()
                    )
                # Note: Skip if no session_id to avoid project_id=None sessions
            except Exception as e:
                print(f"[AgentLLMClient] Failed to save user message to memory: {e}")
        
        # Add to history manager
        self.history_manager.add_message("user", user_input)
        
        context = self._build_conversation_context()
        
        # Check if reasoning mode is needed
        if self.reasoning_manager and self.reasoning_manager.is_reasoning_required(user_input, self._get_available_tools()):
            print(f"[AgentLLMClient] 推論モードを使用します")
            
            # Create progress callback if needed
            progress_callback = None  # TODO: Implement progress callback if needed
            
            # Execute in reasoning mode
            response = await self.reasoning_manager.execute_reasoning_mode(
                user_input=user_input,
                context={
                    'available_tools': self._get_available_tools(),
                    'available_tools': self._get_available_tools(),
                    'conversation_history': self.history_manager.get_all(),
                    'character_name': self.character_name
                },
                progress_callback=progress_callback
            )
            
            # Add assistant response to memory
            if self.memory_manager:
                try:
                    if self.current_session_id:
                        # Use session-specific storage when session ID is set
                        await self.memory_manager.add_message_to_session(
                            session_id=self.current_session_id,
                            role="assistant",
                            content=response,
                            metadata=self._get_memory_metadata()
                        )
                    # Note: Skip if no session_id to avoid project_id=None sessions
                except Exception as e:
                    print(f"[AgentLLMClient] Failed to save assistant message to memory: {e}")
            
            self.history_manager.add_message("assistant", response)
            return response
        
        # Normal execution
        try:
            result = await Runner.run(self.agent, context)
            
            # Log tool usage from the result
            if hasattr(result, 'messages'):
                tool_calls = [msg for msg in result.messages if hasattr(msg, 'role') and msg.role == 'tool']
                print(f"[AgentLLMClient] Tool messages found: {len(tool_calls)}")
                if tool_calls:
                    for tc in tool_calls[:2]:  # Show first 2 tool calls
                        print(f"[AgentLLMClient] Tool call: {tc.tool_name if hasattr(tc, 'tool_name') else 'unknown'}")
            response = result.final_output
        except Exception as e:
            print(f"[AgentLLMClient] Runner.run エラー詳細: {e}")
            import traceback
            traceback.print_exc()
            raise
        
        # Add assistant response to memory
        if self.memory_manager:
            try:
                if self.current_session_id:
                    # Use session-specific storage when session ID is set
                    await self.memory_manager.add_message_to_session(
                        session_id=self.current_session_id,
                        role="assistant",
                        content=response,
                        metadata=self._get_memory_metadata()
                    )
                # Note: Skip if no session_id to avoid project_id=None sessions
            except Exception as e:
                print(f"[AgentLLMClient] Failed to save assistant message to memory: {e}")
        
        self.history_manager.add_message("assistant", response)
        
        # Check if summarization is needed (background task)
        self.check_and_summarize_history(self.history_manager)

        return response

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
                
                print(f"[AgentLLMClient] Summarization task started (History: {len(history_manager.history)})")
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
            messages_to_summarize = history_manager.pop_oldest(self._summarize_batch_size)
            if not messages_to_summarize:
                return

            print(f"[AgentLLMClient] Summarizing {len(messages_to_summarize)} messages...")
            
            # Get current summary
            current_summary = history_manager.summary
            
            # Generate new summary
            new_summary = await self._generate_summary(messages_to_summarize, current_summary)
            
            # Update history manager
            history_manager.update_summary(new_summary)
            print(f"[AgentLLMClient] Summary updated. New history length: {len(history_manager.history)}")
            
        except Exception as e:
            print(f"[AgentLLMClient] Summarization failed: {e}")
            import traceback
            traceback.print_exc()

    async def _generate_summary(self, messages: List[Dict[str, Any]], current_summary: str) -> str:
        """Generate summary using the LLM."""
        
        # Format messages
        conversation_text = ""
        for msg in messages:
            role = "User" if msg['role'] == 'user' else "Assistant"
            conversation_text += f"{role}: {msg['content']}\n"
            
        # Build prompt
        prompt = f'''
以下の「これまでの要約」と「追加の会話」を統合し、新しい要約を作成してください。
要約は、会話の文脈や重要な事実（ユーザーの好み、決定事項、話題の変遷など）を保持するように詳細かつ簡潔にまとめてください。

【これまでの要約】
{current_summary if current_summary else "（なし）"}

【追加の会話】
{conversation_text}

【新しい要約】
'''
        try:
            # Use Runner directly to bypass tools and history
            result = await Runner.run(self.agent, prompt)
            return result.final_output
            
        except Exception as e:
            print(f"[AgentLLMClient] Error generating summary: {e}")
            raise e
    
    async def generate_response_async(self, user_input: str, temperature: float = 0.7, max_tokens: Optional[int] = None) -> str:
        """Async version of generate_response
        
        Args:
            user_input: User's input text
            temperature: Sampling temperature (kept for compatibility)
            max_tokens: Maximum tokens (kept for compatibility)
            
        Returns:
            Generated response
        """
        return await self._generate_async(user_input)
    
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
        tool_names = []
        
        if hasattr(self.agent, 'tools'):
            for tool in self.agent.tools:
                if hasattr(tool, 'name'):
                    tool_names.append(tool.name)
                elif hasattr(tool, '__name__'):
                    tool_names.append(tool.__name__)
        
        return tool_names
    
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
        
        # Clean up MCP plugin
        if self._mcp_initialized:
            try:
                await self.mcp_plugin.cleanup()
                self._mcp_initialized = False
                print("[AgentLLMClient] MCP plugin cleaned up")
            except Exception as e:
                print(f"[AgentLLMClient] Error during MCP cleanup: {e}")
    
    async def add_mcp_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """Add an MCP server dynamically
        
        Args:
            name: Server identifier
            command: Path to server executable
            args: Command line arguments
            env: Environment variables
        """
        if not self._mcp_initialized:
            await self._initialize_mcp()
        
        if self._mcp_initialized:
            success = await self.mcp_plugin.add_server(name, command, args, env)
            if success:
                print(f"[AgentLLMClient] MCP server '{name}' added successfully")
                # Note: Would need to recreate agent to include new tools
                # For now, tools will be available on next conversation
            return success
        return False
    
    def is_mcp_available(self) -> bool:
        """Check if MCP is available and initialized"""
        return self.mcp_plugin.is_available() and self._mcp_initialized


    def _register_cleanup(self):
        """Register cleanup handler for process exit"""
        import atexit
        import signal
        
        def sync_cleanup():
            """Synchronous cleanup wrapper"""
            if self._mcp_initialized and not self._cleanup_registered:
                # Run async cleanup in a new event loop
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


def create_llm_client(config: Config, use_agent: bool = True) -> Union[AgentLLMClient, 'GeminiAgentLLMClient', 'GeminiLLMClient', 'CLILLMClient']:
    """Factory function to create LLM client

    Args:
        config: Application configuration
        use_agent: Whether to use agent SDK (ignored for CLI-based providers)

    Returns:
        Configured LLM client instance
    """
    llm_provider = config.get('llm_provider', 'openai').lower()

    if llm_provider == 'gemini':
        print(f"[LLM Factory] Geminiクライアントを作成")
        from .gemini_engine import create_gemini_client
        return create_gemini_client(config)

    elif llm_provider == 'sglang':
        print(f"[LLM Factory] SGLangクライアントを作成")
        from .sglang_engine import create_sglang_client
        return create_sglang_client(config)

    elif llm_provider in ['gemini-cli', 'claude-cli', 'codex-cli']:
        # CLI-based providers
        print(f"[LLM Factory] {llm_provider.upper()} Backendを作成")

        # Select appropriate CLI backend
        if llm_provider == 'gemini-cli':
            from .cli_backends.gemini import GeminiCLIBackend as CLIImpl
            cli_backend = CLIImpl()
        elif llm_provider == 'claude-cli':
            from .cli_backends.claude import ClaudeCLIBackend as CLIImpl
            cli_backend = CLIImpl(model=config.get('llm_model'))
        elif llm_provider == 'codex-cli':
            from .cli_backends.codex import CodexCLIBackend as CLIImpl
            cli_backend = CLIImpl()

        from .cli_llm_client import CLILLMClient
        return CLILLMClient(config=config, cli_backend=cli_backend)

    else:  # openai or default
        print(f"[LLM Factory] OpenAI Agentクライアントを作成")
        return AgentLLMClient(
            api_key=config.get('openai_api_key'),
            model=config.get('llm_model', 'gpt-4o'),
            use_agent=use_agent,
            config=config
        )
