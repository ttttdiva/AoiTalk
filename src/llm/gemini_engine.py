"""
Gemini LLM engine implementation with Function Calling support
"""
import os
import asyncio
import threading
import concurrent.futures
from typing import Optional, List, Dict, Any, Union, Generator
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold, FunctionDeclaration, Tool

from ..config import Config
from ..tools.registry import get_registry
from ..tools.adapters import GeminiAdapter
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
from ..services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    get_runtime_project_context,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.context_builder import ContextBuilder, ContextBundle
from ..services.scenario_chat_context import (
    build_scenario_chat_context,
    is_scenario_workflow_tool_allowed,
)
from .prompts import build_unified_instructions
from .runtime_tool_registry import build_runtime_tool_registry
from .generation_policy import (
    DEFAULT_GENERATION_POLICY,
    get_client_generation_policy,
    reset_current_generation_policy,
    set_current_generation_policy,
)
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
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall
from ..services.user_settings_service import get_user_custom_instructions_sync


class GeminiLLMClient:
    """Gemini LLM client for character-based responses"""
    
    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview", config: Optional[Config] = None):
        """Initialize Gemini LLM client with Function Calling support
        
        Args:
            api_key: Google AI API key
            model: Gemini model to use
            config: Application configuration
        """
        self.config = config
        self.character_name = config.default_character if config else "Assistant"
        self.conversation_history = []
        self.model_name = model
        self.session_user_id = "default_user"
        self.session_metadata: Dict[str, Any] = {}
        self.current_session_id: Optional[str] = None  # For session-specific message storage and history loading
        self.current_project_id: Optional[str] = None  # For project-specific session creation
        self.generation_policy = DEFAULT_GENERATION_POLICY
        self.current_edit_message_id: Optional[str] = None
        self._current_context_bundle: Optional[ContextBundle] = None
        self._loaded_session_id: Optional[str] = None  # Track which session's history is already loaded
        self._history_lock = threading.Lock()  # Protect conversation_history from concurrent access

        # Initialize memory manager
        self.memory_manager = None
        self._memory_enabled = config.get('memory', {}).get('enabled', True) if config else True
        self._cleanup_done = False
        self._memory_loop: Optional[asyncio.AbstractEventLoop] = None
        self._memory_thread: Optional[threading.Thread] = None
        if self._memory_enabled:
            memory_config = MemoryConfig()
            if config:
                memory_settings = config.get("memory", {})
                memory_config.llm_provider = config.get(
                    'llm_provider', memory_config.llm_provider
                )
                memory_config.llm_model = config.get(
                    'llm_model', memory_config.llm_model
                )
                memory_config.enable_search = memory_settings.get("enable_search", False)
                memory_config.preload_embedding_model = memory_settings.get(
                    "preload_embedding_model", False
                )
            self.memory_manager = ConversationMemoryManager(memory_config)

            # Start persistent memory event loop thread
            self._memory_loop = asyncio.new_event_loop()
            self._memory_thread = threading.Thread(
                target=self._run_memory_loop, daemon=True, name="gemini-memory-loop"
            )
            self._memory_thread.start()

            memory_settings = config.get("memory", {}) if config else {}
            if memory_settings.get("enable_search", False):
                # Pre-warm cross-session memory only when semantic memory search is enabled.
                asyncio.run_coroutine_threadsafe(
                    self._warmup_cross_session_memory(), self._memory_loop
                )

        # Configure Gemini
        genai.configure(api_key=api_key)
        
        # Initialize system prompt based on character
        self.system_prompt = self._build_system_prompt()
        self._tool_registry = (
            build_runtime_tool_registry(config)
            if config
            else get_registry()
        )
        
        # Initialize available tools from unified registry
        self.tools = self._setup_tools()
        
        # Initialize model with safety settings and tools
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        self.model = genai.GenerativeModel(
            model_name=model,
            safety_settings=safety_settings,
            tools=self.tools
        )
        
        print(f"[GeminiLLMClient] モデル初期化: {model}")
        
        # Initialize Spotify
        if self.config:
            from ..tools.entertainment.spotify_tools import init_spotify_manager
            spotify_success = init_spotify_manager()
            if spotify_success:
                print(f"[GeminiLLMClient] Spotify初期化成功")
            else:
                print(f"[GeminiLLMClient] Spotify初期化スキップ（設定不完全）")
        
        print(f"[GeminiLLMClient] Geminiクライアント初期化: {self.character_name}")
        print(f"[GeminiLLMClient] 使用モデル: {model}")
        print(f"[GeminiLLMClient] 利用可能ツール数: {len(self._tool_registry)}")
    
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
            print(f"[GeminiLLMClient] ツール初期化エラー: {e}")
            return []
    
    def _build_system_prompt(self) -> str:
        """Build system prompt based on character configuration"""
        return build_unified_instructions(
            character_name=self.character_name,
            config=self.config,
            custom_instructions=get_user_custom_instructions_sync(
                self._get_session_user_id()
            ),
            include_static_tool_reference=False,
        )

    def _build_effective_system_prompt(self, scenario_context=None) -> str:
        if scenario_context:
            return scenario_context.prompt
        return self._build_system_prompt()

    def _get_effective_gemini_tools(self, scenario_context=None) -> List[Tool]:
        if not scenario_context:
            return self.tools

        filtered_tools: List[Tool] = []
        for tool in self.tools:
            declarations = list(getattr(tool, "function_declarations", []) or [])
            kept = [
                declaration
                for declaration in declarations
                if is_scenario_workflow_tool_allowed(
                    str(getattr(declaration, "name", "")),
                    scenario_context,
                )
            ]
            if kept:
                filtered_tools.append(Tool(function_declarations=kept))
        return filtered_tools
    
    def transcribe_audio(self, file_path) -> Optional[str]:
        """Transcribe audio file to text using Gemini
        
        Args:
            file_path: Path to audio file (str or Path)
            
        Returns:
            Transcribed text or None on error
        """
        try:
            from pathlib import Path
            
            # Convert to Path if needed
            if isinstance(file_path, str):
                file_path = Path(file_path)
            
            print(f"[GeminiLLMClient] Transcribing audio: {file_path}")
            
            # Upload audio file using existing genai configuration
            audio_file = genai.upload_file(path=str(file_path))
            
            # Create a simple model for transcription (or reuse existing)
            # Note: We use a fresh model instance for transcription to avoid tool conflicts
            transcription_model = genai.GenerativeModel("gemini-2.5-flash")
            
            # Generate transcription
            prompt = "Please transcribe the speech in this audio file. Output only the transcribed text without any additional explanation."
            response = transcription_model.generate_content([prompt, audio_file])
            
            # Extract text
            transcription = response.text.strip()
            
            print(f"[GeminiLLMClient] Transcription successful: {len(transcription)} chars")
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
        self.system_prompt = self._build_system_prompt()

    def set_session_context(self, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        """Update identifiers used for persistent memory logging."""
        if user_id:
            self.session_user_id = str(user_id)
        if metadata:
            sanitized = {k: str(v) for k, v in metadata.items() if v is not None}
            self.session_metadata = {**self.session_metadata, **sanitized}

    def _get_session_user_id(self) -> str:
        return self.session_user_id or "default_user"

    def _get_memory_metadata(self) -> Dict[str, Any]:
        return self.session_metadata.copy() if self.session_metadata else {}
        print(f"[GeminiLLMClient] キャラクター変更: {character_name}")
    
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
                self.system_prompt = self._build_system_prompt()
                print(f"[GeminiLLMClient] キャラクター更新: {self.character_name} (会話履歴クリア済み)")
            else:
                print(f"[GeminiLLMClient] キャラクター設定が見つかりません: {yaml_filename}")
        else:
            print(f"[GeminiLLMClient] 設定オブジェクトがありません")
    
    def set_system_prompt(self, prompt: str):
        """Set custom system prompt
        
        Args:
            prompt: System prompt
        """
        self.system_prompt = prompt
        print(f"[GeminiLLMClient] システムプロンプト設定")
    
    def set_llm_mode(self, mode: str):
        """Set LLM response mode
        
        Args:
            mode: 'fast' for quick responses, 'thinking' for deeper reasoning
        
        Note: For Gemini 2.5/3.0, thinking mode uses thinking_config parameter
        """
        if mode not in ['fast', 'thinking']:
            print(f"[GeminiLLMClient] Invalid mode '{mode}', defaulting to 'fast'")
            mode = 'fast'
        
        self._thinking_mode = (mode == 'thinking')
        print(f"[GeminiLLMClient] LLM mode set to: {mode}")
    
    def get_llm_mode(self) -> str:
        """Get current LLM response mode
        
        Returns:
            Current mode ('fast' or 'thinking')
        """
        return 'thinking' if getattr(self, '_thinking_mode', False) else 'fast'

    
    def _execute_tool(self, function_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool function and return its result"""
        try:
            print(f"[GeminiLLMClient] ツール実行: {function_name} with {arguments}")

            scenario_context = self._get_scenario_chat_context_sync()
            if scenario_context and not is_scenario_workflow_tool_allowed(
                function_name,
                scenario_context,
            ):
                return (
                    f"{function_name} is not available in this scenario "
                    f"{scenario_context.mode} session. Continue using the scenario "
                    "workflow context only."
                )

            # 統一レジストリからツール取得・実行
            registry = self._tool_registry
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
                
            except Exception as e:
                error_msg = f"ツール実行エラー ({function_name}): {str(e)}"
                print(f"[GeminiLLMClient] {error_msg}")
                return error_msg
            
        except Exception as e:
            error_msg = f"ツール実行エラー ({function_name}): {str(e)}"
            print(f"[GeminiLLMClient] {error_msg}")
            import traceback
            traceback.print_exc()
            return error_msg
    
    def _build_conversation_context(self, user_input: str) -> List[Dict[str, str]]:
        """Build conversation context from history for Gemini chat"""
        messages = []

        scenario_context = self._get_scenario_chat_context_sync()
        # Scenario workflow sessions use a dedicated system prompt instead of
        # the globally selected app-header assistant prompt.
        enhanced_system_prompt = self._build_effective_system_prompt(scenario_context)
        context_builder_block = (
            self._current_context_bundle.render_for_prompt()
            if not scenario_context and self._current_context_bundle
            else ""
        )
        if context_builder_block:
            enhanced_system_prompt = f"{enhanced_system_prompt}\n\n{context_builder_block}"
        include_project_context = bool(
            getattr(self, "current_include_project_context", True)
        )
        project_context = (
            None
            if scenario_context or not include_project_context
            else get_runtime_project_context()
        )
        if project_context and not context_builder_block:
            project_block = format_project_context_for_chat_prompt(project_context)
            if project_block:
                enhanced_system_prompt = f"{enhanced_system_prompt}\n\n{project_block}"

        # シナリオ情報の取得
        from ..services.scenario_service import get_play_session_by_conversation_id
        session_id = getattr(self, "current_session_id", None)
        if session_id and not scenario_context:
            try:
                play_session = self._run_async_sync(get_play_session_by_conversation_id(session_id))
                if play_session:
                    import json
                    scenario_data = {
                        "scenario_title": play_session.get("scenario", {}).get("title"),
                        "current_scene": play_session.get("current_scene", {}).get("title"),
                        "player_state": play_session.get("player_state", {}),
                        "status": play_session.get("status"),
                    }
                    enhanced_system_prompt = f"{enhanced_system_prompt}\n\n## Active TRPG Scenario State:\n{json.dumps(scenario_data, ensure_ascii=False, indent=2)}"
            except Exception as e:
                print(f"[GeminiLLMClient] Failed to get scenario state: {e}")

        # Add enhanced system prompt as the first user message
        messages.append({
            "role": "user",
            "parts": [enhanced_system_prompt]
        })
        messages.append({
            "role": "model",
            "parts": ["了解しました。設定と記憶を理解しました。"]
        })
        
        # Add conversation history (last 10 exchanges)
        if self.conversation_history:
            for msg in self.conversation_history[-20:]:  # Last 10 exchanges (user + assistant)
                if msg["role"] == "user":
                    messages.append({
                        "role": "user", 
                        "parts": [msg['content']]
                    })
                elif msg["role"] == "assistant":
                    messages.append({
                        "role": "model",
                        "parts": [msg['content']]
                    })
        
        # Add current user input
        messages.append({
            "role": "user",
            "parts": [user_input]
        })
        
        return messages

    def _get_routed_model(self, user_input: str) -> Optional[str]:
        return None

    def _resolve_project_context_sync(self) -> Optional[dict[str, Any]]:
        if not self.current_project_id and not self.current_session_id:
            return None

        if self._get_scenario_chat_context_sync():
            return None

        resolver = ProjectContextResolver()
        try:
            return self._run_async_sync(
                resolver.resolve_context(
                    project_id=self.current_project_id,
                    session_id=self.current_session_id,
                )
            )
        except Exception as e:
            print(f"[GeminiLLMClient] Failed to resolve project context: {e}")
            return None

    def _get_scenario_chat_context_sync(self):
        if not self.current_session_id:
            return None
        try:
            return self._run_async_sync(
                build_scenario_chat_context(self.current_session_id)
            )
        except Exception as e:
            print(f"[GeminiLLMClient] Failed to resolve scenario chat context: {e}")
            return None

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
        if self._get_scenario_chat_context_sync():
            return None
        try:
            return self._run_async_sync(
                ContextBuilder().build_context(
                    user_id=self._get_session_user_id(),
                    message=user_input,
                    project_id=self.current_project_id,
                    session_id=self.current_session_id,
                    project_context=project_context,
                    include_project_context=bool(
                        getattr(self, "current_include_project_context", True)
                    ),
                )
            )
        except Exception as e:
            print(f"[GeminiLLMClient] ContextBuilder failed; fallback to basic context: {e}")
            return None

    def _build_tool_hint_context(self, user_input: str) -> str:
        return build_tool_hint_context_sync(
            user_input=user_input,
            registry=self._tool_registry,
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
    ) -> str:
        scenario_context = self._get_scenario_chat_context_sync()
        effective_tools = self._get_effective_gemini_tools(scenario_context)
        if scenario_context:
            review_model = genai.GenerativeModel(
                model_name=self.model_name,
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
        else:
            review_model = self.model
        chat = review_model.start_chat(history=[])
        latest_message: Any = prompt
        tool_calls: list[OpenAIToolCallRecord] = []

        for _ in range(5):
            response = chat.send_message(
                latest_message,
                generation_config=generation_config,
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
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

            if text_parts:
                return guard_tool_execution_claims("".join(text_parts), tool_calls)

            if not function_calls:
                return ""

            function_response_parts = []
            for func_call in function_calls:
                function_name = func_call.name
                arguments = dict(func_call.args) if func_call.args else {}
                result = self._execute_tool(function_name, arguments)
                tool_calls.append(
                    OpenAIToolCallRecord(
                        tool=function_name,
                        arguments=arguments,
                        result=result,
                    )
                )
                function_response_parts.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=function_name,
                            response={"result": result},
                        )
                    )
                )
            latest_message = function_response_parts

        return "ツール実行の上限に達したため、検証を完了できませんでした。"

    def generate_response(self,
                         user_input: str,
                         temperature: float = 0.7,
                         max_tokens: Optional[int] = None,
                         stream: bool = False,
                         image_data: Optional[Dict[str, Any]] = None) -> Union[str, Generator[str, None, None]]:
        """Generate response using Gemini with Function Calling and multimodal support
        
        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            stream: Whether to stream response
            image_data: Optional image data {data: base64 data URL, mimeType: str, name: str}
            
        Returns:
            Generated response
        """
        # Capture session_id locally to prevent race conditions with concurrent requests
        session_id = self.current_session_id
        edit_message_id = self.current_edit_message_id
        external_persistence = bool(getattr(self, "external_persistence_enabled", False))
        project_token = None
        tool_policy_token = set_current_user_input(user_input)
        generation_policy_token = set_current_generation_policy(
            get_client_generation_policy(self)
        )

        try:
            project_context = self._resolve_project_context_sync()
            project_token = set_runtime_project_context(project_context)
            self._current_context_bundle = self._build_context_bundle_sync(
                user_input, project_context
            )

            # Lock protects conversation_history and _loaded_session_id from concurrent access
            with self._history_lock:
                # Load conversation history from database only when session changes
                if session_id and self.memory_manager and self._memory_enabled:
                    if session_id != self._loaded_session_id:
                        try:
                            print(f"[GeminiLLMClient] Loading history for new session: {session_id}")
                            messages = self._safe_memory_operation(self._load_session_history, session_id)
                            if messages is not None:
                                self.conversation_history = messages
                                self._loaded_session_id = session_id
                                print(f"[GeminiLLMClient] Loaded {len(messages)} messages")
                        except Exception as e:
                            print(f"[GeminiLLMClient] Failed to load session history: {e}")

                # Build conversation context (reads conversation_history)
                context = self._build_conversation_context(user_input)

            model_user_input = user_input
            tool_hint_context = self._build_tool_hint_context(
                user_input
            )
            model_user_input = compose_tool_hint_user_message(
                user_input,
                tool_hint_context,
            )
            if tool_hint_context:
                if context:
                    context[-1]["parts"] = [model_user_input]

            # Initialize memory manager if needed and save user message (outside lock, fire-and-forget)
            if self.memory_manager and self._memory_enabled and not external_persistence:
                try:
                    if session_id:
                        # Use session-specific storage (fire-and-forget for speed)
                        self._safe_memory_operation(
                            self._save_user_message_to_session, user_input, session_id, edit_message_id,
                            fire_and_forget=True
                        )
                    # Note: If no session_id, we skip saving to avoid creating project_id=None sessions
                    # The session should be created by frontend via API call to /api/conversations
                except Exception as e:
                    print(f"[GeminiLLMClient] Failed to save user message to memory: {e}")
            
            # Clear recent tool calls for new request
            self._recent_tool_calls = []
            
            # Apply mode-specific parameters
            thinking_mode = getattr(self, '_thinking_mode', False)
            
            # Adjust temperature based on mode
            if thinking_mode:
                # Thinking mode: lower temperature for more focused reasoning
                effective_temperature = 0.6
            else:
                # Fast mode: use provided temperature
                effective_temperature = temperature
            
            # Generate configuration
            generation_config_kwargs = {
                'temperature': effective_temperature,
                'max_output_tokens': max_tokens or (2048 if thinking_mode else 1024),
                'candidate_count': 1,
            }
            
            # Add thinking_config if in thinking mode (Gemini 2.5+ / 3.0+)
            # This will be tried first; if the model doesn't support it,
            # we'll catch the error and retry without thinking_config
            use_thinking_config = False
            if thinking_mode:
                try:
                    # Check if GenerationConfig supports thinking_config
                    import inspect
                    gen_config_params = inspect.signature(genai.types.GenerationConfig).parameters
                    if 'thinking_config' in gen_config_params:
                        generation_config_kwargs['thinking_config'] = {
                            'thinking_budget': 2048  # Token budget for thinking
                        }
                        use_thinking_config = True
                        print(f"[GeminiLLMClient] Thinking mode enabled with budget: 2048 tokens")
                    else:
                        print(f"[GeminiLLMClient] Model doesn't support thinking_config, using standard mode with lower temperature")
                except Exception as e:
                    print(f"[GeminiLLMClient] thinking_config check failed, using standard mode: {e}")
            
            generation_config = genai.types.GenerationConfig(**generation_config_kwargs)


            
            # モデルルーティング: 有効なら動的モデル選択
            scenario_context = self._get_scenario_chat_context_sync()
            effective_tools = self._get_effective_gemini_tools(scenario_context)
            routed_model = self._get_routed_model(user_input)
            if routed_model and routed_model != self.model_name:
                print(f"[GeminiLLMClient] モデルルーティング: {self.model_name} → {routed_model}")
                routed_genai_model = genai.GenerativeModel(
                    model_name=routed_model,
                    safety_settings=self.model._safety_settings if hasattr(self.model, '_safety_settings') else None,
                    tools=effective_tools if effective_tools else None,
                    system_instruction=self.model._system_instruction if hasattr(self.model, '_system_instruction') else None,
                )
                chat = routed_genai_model.start_chat(history=context[:-1])
            elif scenario_context:
                scenario_model = genai.GenerativeModel(
                    model_name=self.model_name,
                    safety_settings=self.model._safety_settings if hasattr(self.model, '_safety_settings') else None,
                    tools=effective_tools if effective_tools else None,
                    system_instruction=self.model._system_instruction if hasattr(self.model, '_system_instruction') else None,
                )
                chat = scenario_model.start_chat(history=context[:-1])
            else:
                chat = self.model.start_chat(history=context[:-1])  # All except last message
            
            max_tool_calls = 5  # Prevent infinite loops (increased from 3 to support multi-step operations)
            tool_call_count = 0
            
            # Build the message content - handle multimodal input
            message_parts = []
            
            # Add image if provided
            if image_data:
                import base64
                from google.generativeai import protos
                data_url = image_data.get("data", "")
                if data_url.startswith("data:"):
                    # Extract Base64 portion from data URL
                    try:
                        header, encoded = data_url.split(",", 1)
                        mime_type = image_data.get("mimeType", "image/jpeg")
                        
                        # Decode base64 to bytes
                        image_bytes = base64.b64decode(encoded)
                        
                        # Create Gemini Part with inline_data Blob
                        image_part = protos.Part(
                            inline_data=protos.Blob(
                                mime_type=mime_type,
                                data=image_bytes
                            )
                        )
                        message_parts.append(image_part)
                        
                        print(f"[GeminiLLMClient] 画像添付あり: {image_data.get('name', 'unknown')} ({mime_type}, {len(image_bytes)} bytes)")
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
                # Send the latest message
                try:
                    response = chat.send_message(
                        latest_message,
                        generation_config=generation_config
                    )
                except Exception as e:
                    print(f"[GeminiLLMClient] Gemini API呼び出しエラー: {e}")
                    # フォールバック応答
                    fallback = self._get_fallback_response()
                    self.conversation_history.append({"role": "user", "content": user_input})
                    self.conversation_history.append({"role": "assistant", "content": fallback})
                    
                    if stream:
                        def error_generator():
                            yield fallback
                        return error_generator()
                    return fallback
                
                # Check if the response contains function calls - safely check candidates
                try:
                    candidates = getattr(response, 'candidates', [])
                    if not candidates or len(candidates) == 0:
                        print(f"[GeminiLLMClient] 警告: レスポンスにcandidatesがありません")
                        break
                    
                    candidate = candidates[0]
                    if not hasattr(candidate, 'content') or not candidate.content:
                        print(f"[GeminiLLMClient] 警告: candidateにcontentがありません")
                        break
                        
                    if not hasattr(candidate.content, 'parts') or not candidate.content.parts:
                        print(f"[GeminiLLMClient] 警告: contentにpartsがありません")
                        break
                    
                    parts = candidate.content.parts
                except Exception as e:
                    print(f"[GeminiLLMClient] レスポンス解析エラー: {e}")
                    break
                
                # Look for function calls
                function_calls = []
                text_parts = []
                
                for part in parts:
                    if hasattr(part, 'function_call') and part.function_call:
                        function_calls.append(part.function_call)
                    elif hasattr(part, 'text') and part.text:
                        text_parts.append(part.text)
                
                if function_calls:
                    # Execute function calls
                    tool_call_count += 1
                    function_results = []
                    results_text = []
                    
                    # Initialize generated images list for this turn
                    generated_image_tags = []
                    
                    # Track tool calls to detect duplicates
                    current_calls = []
                    duplicate_detected = False
                    
                    for func_call in function_calls:
                        function_name = func_call.name
                        arguments = dict(func_call.args) if func_call.args else {}
                        
                        # Create signature for duplicate detection
                        call_signature = (function_name, tuple(sorted(arguments.items())))
                        
                        # Check for duplicate calls within this session
                        if not hasattr(self, '_recent_tool_calls'):
                            self._recent_tool_calls = []
                        
                        if call_signature in self._recent_tool_calls:
                            print(f"[GeminiLLMClient] 重複ツール呼び出しを検出: {function_name} - スキップしてLLMに指示を送ります")
                            
                            # Instead of breaking, send a system instruction back to the model
                            # This forces the model to use the previous results
                            result = "システム通知: このツールは既に実行済みで、結果は取得されています。これ以上同じ検索を行わず、直前のステップで得られた検索結果「のみ」を使用して、ユーザーの質問に回答してください。"
                            results_text.append(result)
                            
                            function_results.append({
                                "function_response": {
                                    "name": function_name,
                                    "response": {"result": result}
                                }
                            })
                            continue
                        
                        # Add to recent calls
                        self._recent_tool_calls.append(call_signature)
                        current_calls.append(call_signature)
                        
                        # Execute the tool
                        result = self._execute_tool(function_name, arguments)
                        results_text.append(result)
                        all_tool_results.append(result)  # Accumulate across iterations
                        
                        # Track generated images content
                        if function_name == "generate_image":
                            # result is [GENERATED_IMAGE:path]
                            generated_image_tags.append(result)
                        
                        # Prepare function response
                        function_results.append({
                            "function_response": {
                                "name": function_name,
                                "response": {"result": result}
                            }
                        })
                    
                    # Clear recent calls after successful non-duplicate execution
                    if tool_call_count >= max_tool_calls:
                        self._recent_tool_calls = []
                    
                    # For queue operations, return immediately.
                    if len(function_calls) == 1 and user_input and ("キューに" in user_input or "追加" in user_input):
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
                                        name=func_name,
                                        response=func_result
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
                    response_text = re.sub(r'\{get_generated_image_html\(.*?\)\}', '', response_text).strip()
                    
                    # Append any generated image tags to the final response
                    if 'generated_image_tags' in locals() and generated_image_tags:
                        response_text += "\n" + "\n".join(generated_image_tags)

                    response_text = run_agentic_completion_loop_sync(
                        client=self,
                        run_once=lambda review_prompt: self._run_agentic_review_once(
                            review_prompt,
                            generation_config=generation_config,
                            user_input=user_input,
                        ),
                        context=self._render_gemini_context_for_review(
                            context,
                            latest_message,
                        ),
                        user_input=user_input,
                        initial_response=response_text,
                    )

                    # Add to history (under lock to prevent interleaving with concurrent requests)
                    with self._history_lock:
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": response_text})
                    
                    # Save assistant response to memory
                    if self.memory_manager and self._memory_enabled and not external_persistence:
                        try:
                            if session_id:
                                # Use session-specific storage (fire-and-forget for speed)
                                self._safe_memory_operation(
                                    self._save_assistant_message_to_session, response_text, session_id,
                                    fire_and_forget=True
                                )
                            # Note: Skip saving if no session_id to avoid project_id=None sessions
                        except Exception as e:
                            print(f"[GeminiLLMClient] Failed to save assistant message to memory: {e}")
                    
                    # Semantic memory processing now handled by ResponseHandler
                    
                    print(f"[GeminiLLMClient] 応答生成 (ツール呼び出し{tool_call_count}回): {len(response_text)}文字")
                    
                    if stream:
                        def response_generator():
                            yield response_text
                        return response_generator()
                    return response_text
                
                # If no function calls and no text, break
                break
            
            # If we exhausted max_tool_calls but have tool results, try to get a final response
            if tool_call_count >= max_tool_calls and all_tool_results:
                print(f"[GeminiLLMClient] ツール呼び出し上限({max_tool_calls})に達しました。最終応答を生成します...")
                try:
                    # Send a prompt asking for final response based on all the tool results
                    final_prompt = "上記のツール実行結果を使って、ユーザーの質問に対する回答を生成してください。"
                    final_response = chat.send_message(
                        final_prompt,
                        generation_config=generation_config
                    )
                    
                    if final_response.candidates and final_response.candidates[0].content.parts:
                        for part in final_response.candidates[0].content.parts:
                            if hasattr(part, 'text') and part.text:
                                response_text = part.text
                                response_text = run_agentic_completion_loop_sync(
                                    client=self,
                                    run_once=lambda review_prompt: self._run_agentic_review_once(
                                        review_prompt,
                                        generation_config=generation_config,
                                        user_input=user_input,
                                    ),
                                    context=self._render_gemini_context_for_review(
                                        context,
                                        latest_message,
                                    ),
                                    user_input=user_input,
                                    initial_response=response_text,
                                )
                                self.conversation_history.append({"role": "user", "content": user_input})
                                self.conversation_history.append({"role": "assistant", "content": response_text})
                                print(f"[GeminiLLMClient] 最終応答生成: {len(response_text)}文字")
                                
                                if stream:
                                    def response_generator():
                                        yield response_text
                                    return response_generator()
                                return response_text
                except Exception as e:
                    print(f"[GeminiLLMClient] 最終応答生成エラー: {e}")
            
            # Fallback if no valid response
            fallback = self._get_fallback_response()
            self.conversation_history.append({"role": "user", "content": user_input})
            self.conversation_history.append({"role": "assistant", "content": fallback})
            
            if stream:
                def fallback_generator():
                    yield fallback
                return fallback_generator()
            return fallback
                
        except Exception as e:
            print(f"[GeminiLLMClient] エラー: {e}")
            import traceback
            traceback.print_exc()
            
            fallback = self._get_fallback_response()
            
            # Add to history even on error
            self.conversation_history.append({"role": "user", "content": user_input})
            self.conversation_history.append({"role": "assistant", "content": fallback})
            
            if stream:
                def error_generator():
                    yield fallback
                return error_generator()
            return fallback
        finally:
            reset_current_generation_policy(generation_policy_token)
            reset_current_user_input(tool_policy_token)
            if project_token is not None:
                reset_runtime_project_context(project_token)
            self._current_context_bundle = None
    
    def _get_fallback_response(self) -> str:
        """Get fallback response for errors"""
        if self.config:
            character_config = self.config.get_character_config(self.character_name)
            personality = character_config.get('personality', {})
            return personality.get('fallbackReply', 'すみません、うまく聞き取れませんでした。もう一度お話しください。')
        return 'すみません、うまく聞き取れませんでした。もう一度お話しください。'
    
    def _run_memory_loop(self):
        """Run the persistent memory event loop in a background thread"""
        asyncio.set_event_loop(self._memory_loop)
        self._memory_loop.run_forever()

    async def _warmup_cross_session_memory(self):
        """Pre-initialize cross-session memory (embedding model + Qdrant) at startup"""
        try:
            from ..memory.cross_session_memory import get_cross_session_memory
            csm = get_cross_session_memory()
            await csm.initialize()
            print("[GeminiLLMClient] Cross-session memory pre-initialized")
        except Exception as e:
            print(f"[GeminiLLMClient] Cross-session memory warmup failed: {e}")

    def _safe_memory_operation(self, operation_func, *args, timeout=30, fire_and_forget=False):
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

        future = asyncio.run_coroutine_threadsafe(operation_func(*args), self._memory_loop)

        if fire_and_forget:
            def _on_done(f):
                exc = f.exception()
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
            llm_client=self
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
            llm_client=self
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
            from .manager import ConversationMemoryManager
            messages = await self.memory_manager.repository.get_session_messages(session_id)
            
            # Convert to conversation_history format
            history = []
            for msg in messages:
                history.append({
                    "role": "user" if msg.role == "user" else "assistant",
                    "content": msg.content
                })
            
            return history
        except Exception as e:
            print(f"[GeminiLLMClient] Failed to load session history: {e}")
            return []
    
    async def _save_user_message_to_session(
        self,
        user_input: str,
        session_id: str,
        branch_from_message_id: Optional[str] = None,
    ):
        """Save user message to specific session
        
        Args:
            user_input: User input text
            session_id: Session ID to save to
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()
        
        await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="user",
            content=user_input,
            metadata=self._get_memory_metadata(),
            branch_from_message_id=branch_from_message_id,
        )
    
    async def _save_assistant_message_to_session(self, response_text: str, session_id: str):
        """Save assistant message to specific session
        
        Args:
            response_text: Assistant response text
            session_id: Session ID to save to
        """
        if not self.memory_manager.is_initialized():
            await self.memory_manager.initialize()
        
        await self.memory_manager.add_message_to_session(
            session_id=session_id,
            role="assistant",
            content=response_text,
            metadata=self._get_memory_metadata()
        )
    
    async def generate_response_async(self, user_input: str, temperature: float = 0.7, max_tokens: Optional[int] = None, image_data: Optional[Dict[str, Any]] = None) -> str:
        """Async version of generate_response - Gemini API is synchronous, so this just wraps the sync call
        
        Args:
            user_input: User's input text
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            image_data: Optional image data for multimodal input
            
        Returns:
            Generated response
        """
        # Gemini API is synchronous, so we just call the sync method
        import asyncio
        import functools
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.generate_response, user_input, temperature, max_tokens, stream=False, image_data=image_data))
    
    def clear_history(self):
        """Clear conversation history - session creation is handled by frontend"""
        self.conversation_history = []
        self._loaded_session_id = None  # Force DB reload on next request
        print(f"[GeminiLLMClient] 会話履歴をクリア")
        
        # Note: New session creation is handled by frontend (chat.js/conversation-history.js)
        # via API call to /api/conversations, so we don't create a new session here
        # to avoid creating duplicate sessions without project_id
    
    async def _start_new_memory_session(self):
        """Start a new memory session"""
        if self.memory_manager:
            await self.memory_manager.start_new_session(
                user_id=self._get_session_user_id(),
                character_name=self.character_name
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

        # Stop persistent memory event loop
        if self._memory_loop and self._memory_loop.is_running():
            self._memory_loop.call_soon_threadsafe(self._memory_loop.stop)
        if self._memory_thread and self._memory_thread.is_alive():
            self._memory_thread.join(timeout=5)

        # Clean up memory manager
        if self.memory_manager:
            try:
                await self.memory_manager.cleanup()
                print("[GeminiLLMClient] Memory manager cleaned up")
            except Exception as e:
                print(f"[GeminiLLMClient] Error during memory cleanup: {e}")
        
        print(f"[GeminiLLMClient] クリーンアップ完了")


def create_gemini_client(config: Config) -> GeminiLLMClient:
    """Factory function to create Gemini LLM client
    
    Args:
        config: Application configuration
        
    Returns:
        Configured GeminiLLMClient instance
    """
    api_key = config.get('gemini_api_key') or os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise ValueError("Gemini API key not found in config or environment")
    
    model = config.get('llm_model', 'gemini-3-flash-preview')
    
    return GeminiLLMClient(
        api_key=api_key,
        model=model,
        config=config
    )
