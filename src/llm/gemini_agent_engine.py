"""
Gemini LLM engine implementation using OpenAI Agents SDK
"""
import os
from typing import Optional, List, Dict, Union, Generator
from agents import Agent, Runner
import asyncio
import concurrent.futures

from ..config import Config
from ..tools import create_mcp_tool_wrapper, init_spotify_manager
from ..tools.registry import get_registry
from ..tools.adapters import OpenAIAgentAdapter
from ..tools.core import ToolDefinition
from ..tools.external.mcp_plugin import MCPPlugin
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..memory.cross_session_memory import get_cross_session_memory
# Conditionally import semantic memory to avoid SQLite issues
try:
    from ..memory.semantic_memory import SemanticMemoryManager
    SEMANTIC_MEMORY_AVAILABLE = True
except (ImportError, Exception) as e:
    print(f"[GeminiAgentLLMClient] Semantic memory not available: {e}")
    SEMANTIC_MEMORY_AVAILABLE = False
    SemanticMemoryManager = None
from ..agents import SpotifyAgent

from .prompts import build_unified_instructions
from ..reasoning import ReasoningManager


class GeminiAgentLLMClient:
    """Gemini-based LLM client using OpenAI Agents SDK"""
    
    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview", config: Optional[Config] = None):
        """Initialize Gemini-based agent client
        
        Args:
            api_key: Google AI API key
            model: Gemini model to use
            config: Application configuration
        """
        # Set up Gemini API key for Agents SDK
        os.environ['GOOGLE_API_KEY'] = api_key
        
        self.config = config
        self.model_name = model
        # Handle both Config object and dict
        if hasattr(config, 'default_character'):
            self.character_name = config.default_character
        elif isinstance(config, dict):
            self.character_name = config.get('default_character', "Assistant")
        else:
            self.character_name = "Assistant"
        self.conversation_history = []
        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Union[str, None]] = {}
        
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
            self.memory_manager = ConversationMemoryManager(memory_config)
            
            # Initialize semantic memory (Mem0) if available
            if SEMANTIC_MEMORY_AVAILABLE and SemanticMemoryManager:
                try:
                    self.semantic_memory_manager = SemanticMemoryManager(config)
                except Exception as e:
                    print(f"[GeminiAgentLLMClient] Failed to initialize semantic memory: {e}")
                    self.semantic_memory_manager = None
            else:
                self.semantic_memory_manager = None
        
        # Initialize MCP plugin
        self.mcp_plugin = MCPPlugin()
        self._mcp_initialized = False
        self.available_mcp_servers = {}
        self._cleanup_registered = False
        
        # Initialize MCP synchronously if enabled
        if self.config and self.config.get('mcp_enabled', False):
            print(f"[GeminiAgentLLMClient] MCP初期化を開始...")
            try:
                self._initialize_mcp_sync()
            except Exception as e:
                print(f"[GeminiAgentLLMClient] MCP初期化に失敗しました: {e}")
                print(f"[GeminiAgentLLMClient] MCPなしで続行します...")
        else:
            print(f"[GeminiAgentLLMClient] MCPは無効です")
        
        # Initialize Spotify agent
        self.spotify_agent = SpotifyAgent(model=self.model_name)
        print(f"[GeminiAgentLLMClient] Spotify Agent初期化完了")
        
        # ClickUpはMCP経由に移行済み

        # Initialize reasoning manager
        self.reasoning_manager = None
        if self.config:
            reasoning_config = get_config_value('reasoning', {})
            if reasoning_config.get('enabled', False):
                self.reasoning_manager = ReasoningManager(self, reasoning_config)
                print(f"[GeminiAgentLLMClient] 推論モード初期化完了 (閾値: {reasoning_config.get('complexity_threshold', 0.6)})")
        
        self.agent = self._create_character_agent()
        print(f"[GeminiAgentLLMClient] キャラクターエージェント初期化: {self.character_name}")
        
        # Display available tools summary
        if self._mcp_initialized:
            clickup_tools = len([s for s in self.available_mcp_servers.get('clickup', [])])
            print(f"[GeminiAgentLLMClient] 利用可能ツール: 検索、天気、計算、時間、Spotify音楽アシスタント、ファイルシステム操作、RAG検索、ClickUpタスク管理({clickup_tools}個)")
        else:
            print(f"[GeminiAgentLLMClient] 利用可能ツール: 検索、天気、計算、時間、Spotify音楽アシスタント、ファイルシステム操作、RAG検索")
        
        # Initialize Spotify
        spotify_enabled = get_config_value('spotify', {}).get('enabled', True)
        if self.config and spotify_enabled:
            spotify_success = init_spotify_manager()
            if spotify_success:
                print(f"[GeminiAgentLLMClient] Spotify初期化成功")
            else:
                print(f"[GeminiAgentLLMClient] Spotify初期化スキップ（設定不完全）")
        elif not spotify_enabled:
            print(f"[GeminiAgentLLMClient] Spotify機能は無効化されています")

    def set_session_context(self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Union[str, None]]] = None):
        """Update identifiers used when logging memory."""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def _get_memory_metadata(self) -> Dict[str, Union[str, None]]:
        return self.session_metadata.copy() if self.session_metadata else {}
            
        # Register cleanup handler
        self._register_cleanup()
    
    def _initialize_mcp_sync(self):
        """Initialize MCP plugin synchronously"""
        # Same implementation as AgentLLMClient
        import asyncio
        
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[GeminiAgentLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[GeminiAgentLLMClient] MCP SDK not available")
            return
        
        try:
            # Try to use existing event loop if possible
            try:
                loop = asyncio.get_running_loop()
                print("[GeminiAgentLLMClient] Event loop detected, deferring MCP initialization")
                return
            except RuntimeError:
                # No running loop, we can create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    result = loop.run_until_complete(self._do_mcp_init())
                    if result:
                        print("[GeminiAgentLLMClient] MCP synchronous initialization completed")
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)
                    
        except Exception as e:
            print(f"[GeminiAgentLLMClient] MCP initialization error: {e}")
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
                timeout=60.0  # 60 second timeout
            )
            if success:
                self._mcp_initialized = True
                print("[GeminiAgentLLMClient] MCP plugin initialized successfully")
                
                # Log available MCP tools
                tools = await self.mcp_plugin.get_tools_for_agent()
                if tools:
                    print(f"[GeminiAgentLLMClient] MCP tools available: {len(tools)}")
                    
                    # Create detailed MCP tool instructions
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
                    
                    # Update available MCP capabilities
                    self.available_mcp_servers = servers_tools
                    print(f"[GeminiAgentLLMClient] MCP capabilities detected: {list(servers_tools.keys())}")
                    return True
            else:
                print("[GeminiAgentLLMClient] Failed to initialize MCP plugin")
                return False
        except asyncio.TimeoutError:
            print(f"[GeminiAgentLLMClient] MCP initialization timed out after 60 seconds")
            return False
        except Exception as e:
            print(f"[GeminiAgentLLMClient] Error initializing MCP: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _create_character_agent(self) -> Agent:
        """Create character agent with tools from unified registry + MCP"""
        # 統一レジストリから直接登録ツール（MCP化対象外）を取得し、FunctionToolに変換
        registry = get_registry()
        base_tools = list(OpenAIAgentAdapter.convert_all(registry.get_all()))

        # Spotify agent as tool
        if self.spotify_agent:
            base_tools.append(self.spotify_agent.as_tool())

        # Debug: ツールの登録状態を確認
        print(f"[GeminiAgentLLMClient] Registering {len(base_tools)} tools:")
        for t in base_tools:
            print(f"  - {getattr(t, 'name', getattr(t, '__name__', str(t)))}")

        if not self.config:
            return Agent(
                name="MainAssistant",
                instructions="親切なAIアシスタントです。",
                tools=base_tools,
                model=self.model_name
            )

        # Load character configuration
        character_config = self.config.get_character_config(self.character_name)
        character_name = character_config.get('name', self.character_name)

        instructions = build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            include_mcp_info=self._mcp_initialized,
            available_mcp_servers=self.available_mcp_servers if self._mcp_initialized else None
        )

        # MCPサーバーリストを準備（MCP移行済みツールはここから提供される）
        mcp_servers = []
        if self._mcp_initialized and self.mcp_plugin:
            mcp_servers.append(self.mcp_plugin)

        return Agent(
            name=character_name,
            instructions=instructions,
            tools=base_tools,
            model=self.model_name,
            mcp_servers=mcp_servers
        )
    
    def _build_conversation_context(self) -> str:
        """Build conversation context from history"""
        if not self.conversation_history:
            return ""
        
        current_input = self.conversation_history[-1]["content"]
        
        if len(self.conversation_history) == 1:
            return current_input
        
        context_parts = []
        for msg in self.conversation_history[-11:-1]:
            if msg["role"] == "user":
                context_parts.append(f"ユーザー: {msg['content']}")
            else:
                context_parts.append(f"アシスタント: {msg['content']}")
        
        if context_parts:
            context = f"過去の会話:\n" + "\n".join(context_parts) + f"\n\n現在の質問: {current_input}"
        else:
            context = current_input
            
        return context
    
    def set_character(self, character_name: str):
        """Set character and recreate main agent"""
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
                print(f"[GeminiAgentLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)")
            else:
                print(f"[GeminiAgentLLMClient] キャラクター設定が見つかりません: {yaml_filename}")
        else:
            print(f"[GeminiAgentLLMClient] 設定オブジェクトがありません")
    
    def set_system_prompt(self, prompt: str):
        """Set system prompt by recreating agent with new instructions"""
        # For now, this is a no-op as the agent already has character instructions
        pass
    
    def generate_response(self, 
                         user_input: str,
                         temperature: float = 0.7,
                         max_tokens: Optional[int] = None,
                         stream: bool = False) -> Union[str, Generator[str, None, None]]:
        """Generate response using Gemini via Agents SDK"""
        try:
            # Always use a new event loop in a thread to avoid conflicts
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._run_async_safe, user_input)
                response = future.result(timeout=300)  # 5 minute timeout
            
            print(f"[GeminiAgentLLMClient] 応答: {response}")
            
            if stream:
                # Return as generator for compatibility
                def response_generator():
                    yield response
                return response_generator()
            return response
                
        except concurrent.futures.TimeoutError:
            print(f"[GeminiAgentLLMClient] タイムアウトエラー")
            personality = self.config.get_character_config(self.character_name).get('personality', {}) if self.config else {}
            return personality.get('fallbackReply', 'タイムアウトエラーが発生しました')
        except Exception as e:
            print(f"[GeminiAgentLLMClient] エラー: {e}")
            import traceback
            traceback.print_exc()
            personality = self.config.get_character_config(self.character_name).get('personality', {}) if self.config else {}
            return personality.get('fallbackReply', 'エラーが発生しました')
    
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
                self.agent = self._create_character_agent()
        
        # Initialize memory manager if needed
        if self.memory_manager and not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()
        
        # Add user message to memory
        if self.memory_manager:
            try:
                await self.memory_manager.add_message(
                    user_id=self._get_session_user_id(),
                    character_name=self.character_name,
                    role="user",
                    content=user_input,
                    metadata=self._get_memory_metadata(),
                    llm_client=self
                )
            except Exception as e:
                print(f"[GeminiAgentLLMClient] Failed to save user message to memory: {e}")
        
        self.conversation_history.append({"role": "user", "content": user_input})
        
        # Check for past conversation retrieval
        past_context = ""
        try:
            cross_session_memory = get_cross_session_memory()
            if cross_session_memory.should_search_past_conversations(user_input):
                print(f"[GeminiAgentLLMClient] 過去会話検索をトリガー")
                relevant_results = await cross_session_memory.search_relevant_conversations(
                    user_id=self._get_session_user_id(),
                    query=user_input,
                    current_session_id=getattr(self, 'current_session_id', None)
                )
                if relevant_results:
                    past_context = cross_session_memory.format_memory_context(relevant_results)
                    print(f"[GeminiAgentLLMClient] 過去会話コンテキスト取得: {len(past_context)}文字")
        except Exception as e:
            print(f"[GeminiAgentLLMClient] 過去会話検索エラー: {e}")
        
        context = self._build_conversation_context()
        
        # Inject past conversation context if available
        if past_context:
            context = f"{past_context}\n\n{context}"
        
        # Check if reasoning mode is needed
        if self.reasoning_manager and self.reasoning_manager.is_reasoning_required(user_input, self._get_available_tools()):
            print(f"[GeminiAgentLLMClient] 推論モードを使用します")
            
            # Create progress callback if needed
            progress_callback = None  # TODO: Implement progress callback if needed
            
            # Execute in reasoning mode
            response = await self.reasoning_manager.execute_reasoning_mode(
                user_input=user_input,
                context={
                    'available_tools': self._get_available_tools(),
                    'conversation_history': self.conversation_history,
                    'character_name': self.character_name
                },
                progress_callback=progress_callback
            )
            
            # Add assistant response to memory
            if self.memory_manager:
                try:
                    await self.memory_manager.add_message(
                        user_id=self._get_session_user_id(),
                        character_name=self.character_name,
                        role="assistant",
                        content=response,
                        metadata=self._get_memory_metadata(),
                        llm_client=self
                    )
                except Exception as e:
                    print(f"[GeminiAgentLLMClient] Failed to save assistant message to memory: {e}")
            
            self.conversation_history.append({"role": "assistant", "content": response})
            return response
        
        # Normal execution
        try:
            result = await Runner.run(self.agent, context)
            
            # Log tool usage from the result
            if hasattr(result, 'messages'):
                tool_calls = [msg for msg in result.messages if hasattr(msg, 'role') and msg.role == 'tool']
                print(f"[GeminiAgentLLMClient] Tool messages found: {len(tool_calls)}")
                if tool_calls:
                    for tc in tool_calls[:2]:  # Show first 2 tool calls
                        print(f"[GeminiAgentLLMClient] Tool call: {tc.tool_name if hasattr(tc, 'tool_name') else 'unknown'}")
            response = result.final_output
        except Exception as e:
            print(f"[GeminiAgentLLMClient] Runner.run エラー詳細: {e}")
            import traceback
            traceback.print_exc()
            raise
        
        # Add assistant response to memory
        if self.memory_manager:
            try:
                await self.memory_manager.add_message(
                    user_id=self._get_session_user_id(),
                    character_name=self.character_name,
                    role="assistant",
                    content=response,
                    metadata=self._get_memory_metadata(),
                    llm_client=self
                )
            except Exception as e:
                print(f"[GeminiAgentLLMClient] Failed to save assistant message to memory: {e}")
        
        # Semantic memory processing now handled by ResponseHandler
        
        self.conversation_history.append({"role": "assistant", "content": response})
        
        return response
    
    async def generate_response_async(self, user_input: str, temperature: float = 0.7, max_tokens: Optional[int] = None) -> str:
        """Async version of generate_response"""
        return await self._generate_async(user_input)
    
    def clear_history(self):
        """Clear conversation history"""
        self.conversation_history = []
    
    def get_history(self) -> List[Dict[str, str]]:
        """Get current conversation history"""
        return self.conversation_history.copy()
    
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
    
    async def _initialize_mcp(self):
        """Initialize MCP plugin asynchronously (legacy method)"""
        # Same implementation as AgentLLMClient
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[GeminiAgentLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[GeminiAgentLLMClient] MCP SDK not available")
            return
            
        try:
            # Get MCP configuration from config
            mcp_config = self.config.get('mcp', {}) if self.config else {}
            # Add timeout for MCP initialization
            success = await asyncio.wait_for(
                self.mcp_plugin.initialize(mcp_config),
                timeout=60.0
            )
            if success:
                self._mcp_initialized = True
                print("[GeminiAgentLLMClient] MCP plugin initialized successfully")
                
                # Log available MCP tools
                tools = await self.mcp_plugin.get_tools_for_agent()
                if tools:
                    print(f"[GeminiAgentLLMClient] MCP tools available: {len(tools)}")
                    
                    # Update available MCP capabilities
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
                    
                    self.available_mcp_servers = servers_tools
                    print(f"[GeminiAgentLLMClient] MCP capabilities detected: {list(servers_tools.keys())}")
            else:
                print("[GeminiAgentLLMClient] Failed to initialize MCP plugin")
        except asyncio.TimeoutError:
            print(f"[GeminiAgentLLMClient] MCP initialization timed out after 60 seconds")
        except Exception as e:
            print(f"[GeminiAgentLLMClient] Error initializing MCP: {e}")
            import traceback
            traceback.print_exc()
    
    def _register_cleanup(self):
        """Register cleanup handler"""
        if not self._cleanup_registered:
            import atexit
            atexit.register(self._cleanup)
            self._cleanup_registered = True
    
    def _cleanup(self):
        """Cleanup resources"""
        if hasattr(self, 'memory_manager') and self.memory_manager:
            try:
                # Run async cleanup in sync context
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.memory_manager.cleanup())
                finally:
                    loop.close()
                print("[GeminiAgentLLMClient] Memory manager cleaned up")
            except Exception as e:
                print(f"[GeminiAgentLLMClient] Cleanup error: {e}")
    
    def __del__(self):
        """Destructor to ensure cleanup"""
        if hasattr(self, '_cleanup_done') and not self._cleanup_done:
            self._cleanup()
            self._cleanup_done = True
