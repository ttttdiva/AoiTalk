"""
Gemini LLM engine implementation with Function Calling support
"""
import os
import asyncio
import threading
from typing import Optional, List, Dict, Any, Union, Generator
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold, FunctionDeclaration, Tool

from ..config import Config
from ..tools.registry import get_registry
from ..tools.adapters import GeminiAdapter
from ..memory.manager import ConversationMemoryManager
from ..memory.config import MemoryConfig
# Conditionally import semantic memory to avoid SQLite issues
try:
    from ..memory.semantic_memory import SemanticMemoryManager
    SEMANTIC_MEMORY_AVAILABLE = True
except (ImportError, Exception) as e:
    print(f"[GeminiLLMClient] Semantic memory not available: {e}")
    SEMANTIC_MEMORY_AVAILABLE = False
    SemanticMemoryManager = None
from ..tools.external.mcp_plugin import MCPPlugin


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
        self._loaded_session_id: Optional[str] = None  # Track which session's history is already loaded
        self._history_lock = threading.Lock()  # Protect conversation_history from concurrent access

        # Initialize memory manager
        self.memory_manager = None
        self.semantic_memory_manager = None
        self._memory_enabled = config.get('memory', {}).get('enabled', True) if config else True
        self._cleanup_done = False
        self._memory_loop: Optional[asyncio.AbstractEventLoop] = None
        self._memory_thread: Optional[threading.Thread] = None
        if self._memory_enabled:
            memory_config = MemoryConfig()
            if config:
                # Override with app config if available
                memory_config.llm_provider = config.get('llm_provider', 'gemini')
                memory_config.llm_model = config.get('llm_model', 'gemini-3-flash-preview')
            self.memory_manager = ConversationMemoryManager(memory_config)

            # Initialize semantic memory (Mem0) if available
            if SEMANTIC_MEMORY_AVAILABLE and SemanticMemoryManager:
                try:
                    self.semantic_memory_manager = SemanticMemoryManager(config)
                except Exception as e:
                    print(f"[GeminiLLMClient] Failed to initialize semantic memory: {e}")
                    self.semantic_memory_manager = None
            else:
                self.semantic_memory_manager = None

            # Start persistent memory event loop thread
            self._memory_loop = asyncio.new_event_loop()
            self._memory_thread = threading.Thread(
                target=self._run_memory_loop, daemon=True, name="gemini-memory-loop"
            )
            self._memory_thread.start()

            # Pre-warm cross-session memory (embedding model + Qdrant) in background
            asyncio.run_coroutine_threadsafe(self._warmup_cross_session_memory(), self._memory_loop)

        # Initialize MCP plugin
        self.mcp_plugin = MCPPlugin()
        self._mcp_initialized = False
        
        # Configure Gemini
        genai.configure(api_key=api_key)
        
        # Initialize system prompt based on character
        self.system_prompt = self._build_system_prompt()
        
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
        
        # Initialize MCP plugin synchronously if enabled
        if self.config and self.config.get('mcp_enabled', False):
            print(f"[GeminiLLMClient] MCP初期化を開始...")
            # Force synchronous initialization without event loop checks
            self._force_mcp_init_sync()
        else:
            print(f"[GeminiLLMClient] MCPは無効です")
        
        print(f"[GeminiLLMClient] Geminiクライアント初期化: {self.character_name}")
        print(f"[GeminiLLMClient] 使用モデル: {model}")
        print(f"[GeminiLLMClient] 利用可能ツール数: {len(get_registry())}")
    
    def _setup_tools(self) -> List[Tool]:
        """Setup Function Calling tools for Gemini using the unified registry"""
        try:
            registry = get_registry()
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
        tools_info = "検索、天気、計算、時間確認、記憶検索、音楽、タスク管理などのツールが使えます。"
        memory_info = "過去の会話を質問されたらsearch_memoryで検索してください。"
        # Critical instruction to prevent hallucination with tool results
        tool_result_instruction = "重要：ツールを使用した場合は、ツールから返された結果をそのまま正確に引用して回答してください。結果を勝手に解釈したり、存在しない情報を追加しないでください。"

        # スキル情報を取得
        skills_info = ""
        try:
            from .prompts import _build_skills_section
            skills_section = _build_skills_section()
            if skills_section:
                skills_info = f"\n{skills_section}"
        except Exception:
            pass

        if not self.config:
            return f"親切なAIアシスタントです。{tools_info}{memory_info}{tool_result_instruction}{skills_info}"

        # Load character configuration
        character_config = self.config.get_character_config(self.character_name)
        personality = character_config.get('personality', {})
        character_name = character_config.get('name', self.character_name)

        # Build character instructions
        details = personality.get('details', '')
        return f"あなたは{character_name}です。{details}{tools_info}{memory_info}{tool_result_instruction}{skills_info}"
    
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
            
            # 統一レジストリからツール取得・実行
            registry = get_registry()
            tool_def = registry.get(function_name)
            if not tool_def:
                return f"エラー: 未知の関数 '{function_name}'"

            # ToolDefinition.execute() で実行（同期/非同期を自動処理）
            try:
                if arguments:
                    result = tool_def.execute(**arguments)
                else:
                    result = tool_def.execute()

                print(f"[GeminiLLMClient] ツール結果: {result}")
                return str(result)
                
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
        
        # Get relevant memories for context enhancement
        memory_context = ""
        if SEMANTIC_MEMORY_AVAILABLE and self.semantic_memory_manager and self._memory_enabled:
            try:
                # Only attempt if semantic memory is properly initialized
                if hasattr(self.semantic_memory_manager, '_initialized') and self.semantic_memory_manager._initialized:
                    memory_context = self._safe_memory_operation(self._retrieve_relevant_memories_sync, user_input)
                    if memory_context:
                        print(f"[GeminiLLMClient] 関連記憶を取得: {len(memory_context)}文字")
            except Exception as e:
                print(f"[GeminiLLMClient] Memory retrieval failed: {e}")
        
        # Build enhanced system prompt with memories
        enhanced_system_prompt = self.system_prompt
        if memory_context:
            enhanced_system_prompt = f"{self.system_prompt}\n\n{memory_context}"
        
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

        try:
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

            # Initialize memory manager if needed and save user message (outside lock, fire-and-forget)
            if self.memory_manager and self._memory_enabled:
                try:
                    if session_id:
                        # Use session-specific storage (fire-and-forget for speed)
                        self._safe_memory_operation(
                            self._save_user_message_to_session, user_input, session_id,
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


            
            # Start conversation with the model
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
                message_parts.append(user_input)
            
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
                    
                    # For queue operations, return immediately (keep legacy behavior)
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
                    
                    # Add to history (under lock to prevent interleaving with concurrent requests)
                    with self._history_lock:
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": response_text})
                    
                    # Save assistant response to memory
                    if self.memory_manager and self._memory_enabled:
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
    
    async def _save_user_message_to_session(self, user_input: str, session_id: str):
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
            metadata=self._get_memory_metadata()
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
    
    # Semantic memory processing moved to ResponseHandler for unified handling across all LLM providers
    
    async def _retrieve_relevant_memories(self, user_input: str) -> str:
        """Retrieve relevant memories for context enhancement
        
        Args:
            user_input: User's input to search for relevant memories
            
        Returns:
            Formatted memory context string
        """
        if not SEMANTIC_MEMORY_AVAILABLE or not self.semantic_memory_manager:
            return ""
            
        try:
            # Search for relevant semantic facts
            results = await self.semantic_memory_manager.search_semantic_facts(
                user_id=self._get_session_user_id(),
                character_name=self.character_name,
                query=user_input,
                limit=3
            )
            
            if not results:
                return ""
            
            # Format relevant memories for context
            memory_context = "関連する記憶:\n"
            for result in results:
                memory_context += f"- {result['content']}\n"
            
            return memory_context
            
        except Exception as e:
            print(f"[GeminiLLMClient] Memory retrieval error: {e}")
            return ""
    
    async def _initialize_mcp(self, mcp_config: Dict[str, Any]):
        """Initialize MCP plugin asynchronously
        
        Args:
            mcp_config: MCP configuration from config file
        """
        try:
            if self.mcp_plugin:
                success = await self.mcp_plugin.initialize(mcp_config)
                if success:
                    from ..tools.external.mcp_tools import set_mcp_plugin
                    set_mcp_plugin(self.mcp_plugin)
                    print(f"[GeminiLLMClient] MCP plugin初期化成功")
                    
                    # List available tools from MCP servers
                    tools_info = await self.mcp_plugin.client.list_tools()
                    total_mcp_tools = sum(len(tools) for tools in tools_info.values())
                    print(f"[GeminiLLMClient] MCP tools available: {total_mcp_tools}")
                    
                else:
                    print(f"[GeminiLLMClient] MCP plugin初期化失敗")
        except Exception as e:
            print(f"[GeminiLLMClient] MCP初期化エラー: {e}")

    async def _retrieve_relevant_memories_sync(self, user_input: str) -> str:
        """Async memory retrieval for safe operation
        
        Args:
            user_input: User's input to search for relevant memories
            
        Returns:
            Formatted memory context string
        """
        if not SEMANTIC_MEMORY_AVAILABLE or not self.semantic_memory_manager:
            return ""
            
        try:
            # Search for relevant semantic facts
            results = await self.semantic_memory_manager.search_semantic_facts(
                user_id=self._get_session_user_id(),
                character_name=self.character_name,
                query=user_input,
                limit=3
            )
            
            if not results:
                return ""
            
            # Format memory context
            memory_context = "## 関連する記憶:\n"
            for i, result in enumerate(results[:3], 1):
                content = result.get("content", "")
                score = result.get("relevance_score", 0.0)
                memory_context += f"{i}. {content} (関連度: {score:.2f})\n"
            
            return memory_context
            
        except Exception as e:
            print(f"[GeminiLLMClient] Memory retrieval error: {e}")
            return ""
    
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

    def _force_mcp_init_sync(self):
        """Force MCP initialization synchronously without event loop checks"""
        import asyncio
        
        # Check if MCP is enabled in configuration
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[GeminiLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[GeminiLLMClient] MCP SDK not available")
            return
        
        try:
            # Always create a new event loop for MCP initialization
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                result = loop.run_until_complete(self._do_mcp_init())
                if result:
                    print("[GeminiLLMClient] MCP forced synchronous initialization completed")
            finally:
                # Don't close the loop immediately, keep it for MCP operations
                # loop.close()
                # asyncio.set_event_loop(None)
                pass
                
        except Exception as e:
            print(f"[GeminiLLMClient] MCP initialization error: {e}")
            import traceback
            traceback.print_exc()

    def _initialize_mcp_sync(self):
        """Initialize MCP plugin synchronously"""
        import asyncio
        
        # Check if MCP is enabled in configuration
        if not self.config or not self.config.get('mcp_enabled', False):
            print("[GeminiLLMClient] MCP disabled in configuration")
            return
            
        if not self.mcp_plugin.is_available():
            print("[GeminiLLMClient] MCP SDK not available")
            return
        
        try:
            # Try to use existing event loop if possible
            try:
                loop = asyncio.get_running_loop()
                # If we're in an event loop, defer initialization
                print("[GeminiLLMClient] Event loop detected, deferring MCP initialization")
                return
            except RuntimeError:
                # No running loop, we can create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    result = loop.run_until_complete(self._do_mcp_init())
                    if result:
                        print("[GeminiLLMClient] MCP synchronous initialization completed")
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)
                    
        except Exception as e:
            print(f"[GeminiLLMClient] MCP initialization error: {e}")
            import traceback
            traceback.print_exc()

    async def _do_mcp_init(self):
        """Actual MCP initialization logic"""
        try:
            # Get MCP configuration from config
            mcp_config = self.config.get('mcp', {}) if self.config else {}
            success = await self.mcp_plugin.initialize(mcp_config)
            if success:
                self._mcp_initialized = True
                print("[GeminiLLMClient] MCP plugin initialized successfully")
                
                # Set up MCP plugin for tools
                from ..tools.external.mcp_tools import set_mcp_plugin
                set_mcp_plugin(self.mcp_plugin)
                
                # Log available MCP tools
                tools = await self.mcp_plugin.get_tools_for_agent()
                if tools:
                    print(f"[GeminiLLMClient] MCP tools available: {len(tools)}")
                    
                    for tool in tools:
                        server_name = tool.get('server_name', 'unknown')
                        tool_name = tool.get('tool_name', 'unknown')
                        description = tool['function']['description']
                        print(f"  - {server_name}.{tool_name}: {description}")
                    return True
            else:
                print("[GeminiLLMClient] Failed to initialize MCP plugin")
                return False
        except Exception as e:
            print(f"[GeminiLLMClient] Error initializing MCP: {e}")
            import traceback
            traceback.print_exc()
            return False


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
