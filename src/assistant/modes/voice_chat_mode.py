"""
Voice chat mode for AoiTalk Voice Assistant Framework
"""

import asyncio
import time
import platform
from typing import Optional
from ..base import BaseAssistant
from ..voice_handler import VoiceHandler
from ..response_handler import ResponseHandler
from src.tools.keyword.character_manager import get_character_manager


class VoiceChatMode(BaseAssistant):
    """Voice chat mode assistant - voice recognition + web chat interface"""
    
    def __init__(self, config):
        """Initialize voice chat mode assistant"""
        super().__init__(config, 'voice_chat')
        
        # Initialize placeholders - actual initialization happens in _initialize_mode_specific
        self.recorder = None
        self.recognizer = None
        self.player = None
        self.tts_manager = None
        self.voice_handler = None
        self.response_handler = None
        
        # Web interface
        self.web_interface: Optional[object] = None
        
        # Running flag as mutable reference for voice handler
        self._running_flag = [False]
        
        # Pending engine switch flag
        self._pending_engine_switch = None
        
        # Register TTS character switch callback
        self._register_tts_character_switch_callback()
        
    def _register_tts_character_switch_callback(self):
        """Register callback for TTS character switching"""
        manager = get_character_manager()
        manager.register_callback(self._on_tts_character_switch)
        
    def _on_tts_character_switch(self, character_name: str, yaml_filename: str):
        """Handle TTS character switch event
        
        Args:
            character_name: New character name
            yaml_filename: YAML filename (without extension)
        """
        print(f"[VoiceChatMode] TTSキャラクター切り替え: {character_name}")
        
        # Reload character configuration
        new_config = self.config.get_character_config(character_name)
        if not new_config:
            print(f"[VoiceChatMode] キャラクター設定が見つかりません: {character_name}")
            return
            
        # Update current character info
        self.character_name = character_name
        self.character_config = new_config
        
        # Register character in TTS manager
        self.tts_manager.register_character(character_name, new_config)
        
        # Switch TTS engine if needed
        voice_config = new_config.get('voice', {})
        preferred_engine = voice_config.get('engine', 'voicevox')
        current_engine = self.tts_manager.current_engine
        
        if preferred_engine != current_engine:
            print(f"[VoiceChatMode] TTSエンジン切り替え: {current_engine} -> {preferred_engine}")
            # Set flag to reinitialize engine on next synthesis
            self._pending_engine_switch = preferred_engine
            
            # Check if the engine is already initialized
            if self._pending_engine_switch in self.tts_manager.engines:
                # Just switch to existing engine immediately
                self.tts_manager.set_engine(self._pending_engine_switch)
                print(f"[VoiceChatMode] 既存の{self._pending_engine_switch}エンジンに切り替えました")
                self.tts_manager.prepare_character_voice(character_name)
                self._pending_engine_switch = None
            else:
                # Keep the flag set - it will be handled in _synthesize_with_engine_check
                print(f"[VoiceChatMode] エンジン切り替えフラグを設定: {self._pending_engine_switch}")
        elif preferred_engine == 'voiceroid' and current_engine == 'voiceroid':
            # Force reinitialization to avoid sticking to previous speaker
            print("[VoiceChatMode] VOICEROIDエンジンを再初期化します")
            existing_engine = self.tts_manager.engines.pop('voiceroid', None)
            if existing_engine and hasattr(existing_engine, 'cleanup'):
                try:
                    existing_engine.cleanup()
                except Exception as e:
                    print(f"[VoiceChatMode] VOICEROID cleanup error: {e}")
            self.tts_manager.current_engine = None
            self._pending_engine_switch = 'voiceroid'
        else:
            self._pending_engine_switch = None
            if preferred_engine == 'voiceroid':
                self.tts_manager.prepare_character_voice(character_name)
        
        # Update ResponseHandler's character name
        if hasattr(self, 'response_handler') and self.response_handler:
            self.response_handler.character_name = character_name
        
    async def _initialize_mode_specific(self) -> bool:
        """Initialize voice chat mode specific components"""
        # Initialize audio components
        from src.audio.recorder import AudioRecorder
        from src.audio.manager import SpeechRecognitionManager
        from src.audio.player import AudioPlayer
        from src.tts.manager import TTSManager
        
        self.recorder = AudioRecorder(device_index=self.config.device_index)
        
        # Initialize speech recognition manager
        speech_config = self.config.get('speech_recognition', {})
        engine_name = speech_config.get('current_engine', 'whisper')
        self.recognizer = SpeechRecognitionManager(engine_name, speech_config)
        
        self.player = AudioPlayer()
        self.tts_manager = TTSManager()
        
        # Initialize handlers
        self.voice_handler = VoiceHandler(self.config, self.recognizer, self.player)
        self.response_handler = ResponseHandler(
            self.llm_client,
            self.tts_manager,
            self.player,
            character_name=self.character_name,
            voice_chat_mode=self
        )
        
        # Initialize TTS engine based on character preference
        voice_config = self.character_config.get('voice', {})
        preferred_engine = voice_config.get('engine', 'voicevox')
        
        print(f"TTSエンジン: {preferred_engine}")
        
        # Initialize TTS engine
        engine_initialized = await self._initialize_tts_engine(preferred_engine, self.character_config)
        
        # Setup voice callback
        self.voice_handler.set_audio_callback(self._handle_voice_input)
        
        # Initialize GUI
        self._initialize_gui()
        
        return engine_initialized
    
    async def _initialize_tts_engine(self, preferred_engine: str, character_config: dict) -> bool:
        """Initialize TTS engine
        
        Args:
            preferred_engine: Preferred engine name
            character_config: Character configuration
            
        Returns:
            True if engine initialized successfully
        """
        engine_initialized = False
        
        if preferred_engine == 'voiceroid':
            print("VOICEROIDエンジンを初期化中...")
            voiceroid_engine = await self.tts_manager.create_voiceroid_engine(character_config)
            if voiceroid_engine:
                self.tts_manager.register_engine("voiceroid", voiceroid_engine)
                self.tts_manager.set_engine("voiceroid")
                engine_initialized = True
                print("VOICEROIDエンジンの初期化完了")
            else:
                print("VOICEROIDエンジンの初期化に失敗しました")
                return False
        
        elif preferred_engine == 'aivoice':
            print("A.I.VOICEエンジンを初期化中...")
            aivoice_path = self.config.get('aivoice_engine_path')
            aivoice_engine = await self.tts_manager.create_aivoice_engine(aivoice_path)
            if aivoice_engine:
                self.tts_manager.register_engine("aivoice", aivoice_engine)
                self.tts_manager.set_engine("aivoice")
                engine_initialized = True
                print("A.I.VOICEエンジンの初期化完了")
            else:
                print("A.I.VOICEエンジンの初期化に失敗しました")
                return False
        
        elif preferred_engine == 'aivisspeech':
            print("AivisSpeechエンジンを初期化中...")
            import os
            import platform
            
            # Try primary path first
            aivisspeech_path = os.getenv('AIVISSPEECH_ENGINE_PATH')
            
            # Handle path expansion based on platform
            if aivisspeech_path:
                # Replace Unix-style $HOME with Windows equivalent if needed
                if platform.system() == 'Windows' and '$HOME' in aivisspeech_path:
                    home_path = os.path.expanduser('~')
                    aivisspeech_path = aivisspeech_path.replace('$HOME', home_path)
                
                # Now expand any remaining environment variables
                aivisspeech_path = os.path.expandvars(aivisspeech_path)
                
                # If primary path doesn't exist, try fallback path
                if not os.path.exists(aivisspeech_path):
                    fallback_path = os.getenv('AIVISSPEECH_ENGINE_FALLBACK_PATH')
                    if fallback_path:
                        # Handle Windows path expansion for fallback
                        if platform.system() == 'Windows' and '$HOME' in fallback_path:
                            home_path = os.path.expanduser('~')
                            fallback_path = fallback_path.replace('$HOME', home_path)
                        fallback_path = os.path.expandvars(fallback_path)
                        if os.path.exists(fallback_path):
                            print(f"Using fallback path: {fallback_path}")
                            aivisspeech_path = fallback_path
            else:
                # No primary path, try fallback directly
                fallback_path = os.getenv('AIVISSPEECH_ENGINE_FALLBACK_PATH')
                if fallback_path:
                    # Handle Windows path expansion for fallback
                    if platform.system() == 'Windows' and '$HOME' in fallback_path:
                        home_path = os.path.expanduser('~')
                        fallback_path = fallback_path.replace('$HOME', home_path)
                    aivisspeech_path = os.path.expandvars(fallback_path)
            
            if aivisspeech_path and os.path.exists(aivisspeech_path):
                aivisspeech_engine = await self.tts_manager.create_aivisspeech_engine(aivisspeech_path)
                if aivisspeech_engine:
                    self.tts_manager.register_engine("aivisspeech", aivisspeech_engine)
                    self.tts_manager.set_engine("aivisspeech")
                    engine_initialized = True
                    print("AivisSpeechエンジンの初期化完了")
                    print(f"[VoiceChatMode] AivisSpeechエンジンを登録・設定しました")
                else:
                    print("AivisSpeechエンジンの初期化に失敗しました")
                    return False
            else:
                print(f"AivisSpeechエンジンが見つかりません: {aivisspeech_path}")
                return False
        
        elif preferred_engine == 'nijivoice':
            print("Nijivoiceエンジンを初期化中...")
            nijivoice_api_key = self.config.get('nijivoice_api_key')
            
            if nijivoice_api_key:
                nijivoice_engine = await self.tts_manager.create_nijivoice_engine(nijivoice_api_key)
                if nijivoice_engine:
                    self.tts_manager.register_engine("nijivoice", nijivoice_engine)
                    self.tts_manager.set_engine("nijivoice")
                    engine_initialized = True
                    print("Nijivoiceエンジンの初期化完了")
                else:
                    print("Nijivoiceエンジンの初期化に失敗しました")
                    return False
            else:
                print("NIJIVOICE_API_KEY環境変数が設定されていません")
                return False
        
        # Only use VOICEVOX if explicitly specified
        elif preferred_engine == 'voicevox':
            print("VOICEVOXエンジンを初期化中...")
            voicevox_engine = await self.tts_manager.create_voicevox_engine(
                self.config.voicevox_path
            )
            if voicevox_engine:
                self.tts_manager.register_engine("voicevox", voicevox_engine)
                self.tts_manager.set_engine("voicevox")
                engine_initialized = True
                print("VOICEVOXエンジンの初期化完了")
            else:
                print("VOICEVOXエンジンの初期化に失敗しました")
                return False
        
        elif preferred_engine == 'qwen3tts':
            print("Qwen3-TTSエンジンを初期化中...")
            qwen3_engine = await self.tts_manager.create_qwen3_tts_engine()
            if qwen3_engine:
                self.tts_manager.register_engine("qwen3tts", qwen3_engine)
                self.tts_manager.set_engine("qwen3tts")
                engine_initialized = True
                print("Qwen3-TTSエンジンの初期化完了")
            else:
                print("Qwen3-TTSエンジンの初期化に失敗しました")
                return False
        
        if not engine_initialized:
            import traceback
            print(f"指定されたTTSエンジン '{preferred_engine}' の初期化に失敗しました")
            traceback.print_exc()
            raise
            
        # Register character configuration
        self.tts_manager.register_character(character_config.get('name', 'Unknown'), character_config)
        
        return engine_initialized
        
    def _initialize_gui(self):
        """Initialize GUI components (placeholder for now)"""
        # GUI initialization will be handled in run() method
        pass
        
    async def run(self):
        """Run voice chat mode"""
        # Initialize
        if not await self.initialize():
            return
            
        # Set running flag for voice handler
        self._running_flag[0] = True
        self.running = True
        
        # Get greeting
        personality = self.character_config.get('personality', {})
        greeting = personality.get('greeting', 'こんにちは！')
        
        print(f"\n🎤💬 音声&チャットモード開始（Webベース）")
        print(f"{self.character_name}: {greeting}")
        
        # Initialize web interface
        web_host, web_port, auto_open = self._get_web_interface_settings()
        server_url = self._start_web_interface(
            self._process_user_message_web,
            host=web_host,
            port=web_port,
            auto_open_browser=auto_open
        )
        if not server_url:
            return

        print("🌐 Webチャットインターフェースを開始しました")
        print(f"📍 ブラウザで以下のURLにアクセスしてください: {server_url}")

        # Show device info
        print("\n利用可能な音声デバイス:")
        for device in self.recorder.list_devices():
            mark = ">" if device['index'] == self.config.device_index else " "
            print(f"{mark} [{device['index']}] {device['name']}")
            
        # Show speech recognition engine info
        engine_info = self.recognizer.get_engine_info()
        print(f"\n🎤 音声認識エンジン: {engine_info.get('engine', 'unknown')}")
        print(f"📊 モデル: {engine_info.get('model', 'unknown')}")
        print(f"🌏 言語: {engine_info.get('language', 'unknown')}")
        
        # Show info on web interface
        self.web_interface.add_system_message(f"🎤 音声認識: {engine_info.get('engine', 'unknown')} ({engine_info.get('model', 'unknown')})")
        
        # Set voice recognition ready state
        self.web_interface.set_voice_recognition_ready(True)
        
        # Set transcription callback for immediate UI updates
        async def transcription_callback(text: str):
            """Callback to immediately display transcribed text in UI"""
            if self.web_interface:
                self.web_interface.add_user_message(text)
                
        self.voice_handler.set_transcription_callback(transcription_callback)
        
        # Start voice recognition
        print("\n🎤💬 音声認識とWebチャット対話モード")
        print("💡 マイクに話しかけるかWebブラウザでチャットしてください！")
        print("⚠️  終了するには Ctrl+C を押してください")
        
        # Set RMS callback for web interface
        def rms_callback(rms_value):
            if self.web_interface:
                self.web_interface.update_rms(rms_value)
        
        # Set recording state callback
        original_process_audio_chunk = self.voice_handler._process_audio_chunk
        def enhanced_process_audio_chunk(audio_data, rms, current_segment, voice_detected, 
                                        silence_start, pre_voice_buffer, voice_start_time,
                                        voice_threshold, silence_threshold, silence_duration, CHUNK, RATE):
            
            # Call original method
            result = original_process_audio_chunk(audio_data, rms, current_segment, voice_detected, 
                                                 silence_start, pre_voice_buffer, voice_start_time,
                                                 voice_threshold, silence_threshold, silence_duration, CHUNK, RATE)
            
            # Check if recording state changed
            new_voice_detected = result[0]
            if new_voice_detected != voice_detected:
                if self.web_interface:
                    self.web_interface.set_recording_state(new_voice_detected)
            
            return result
        
        # Replace the method
        self.voice_handler._process_audio_chunk = enhanced_process_audio_chunk
        
        self.voice_handler.start_recording(rms_callback=rms_callback)
        
        # Wait for recording thread to start and noise calibration to complete
        print("\n🎤 ノイズレベル測定中...")
        await asyncio.sleep(3.5)  # 3秒の計測 + 0.5秒の余裕
        
        # Display greeting in web interface
        if self.web_interface:
            self.web_interface.add_assistant_message(greeting)
        
        # Synthesize and play greeting after noise calibration
        print(f"\n{self.character_name}: {greeting}")
        await self._play_greeting(greeting)
        
        # Start audio processing
        asyncio.create_task(self.voice_handler.process_audio_queue(self._running_flag))
        
        # Main loop - handle voice processing and web interface
        try:
            while self.running:
                await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            print("\n🛑 終了シグナルを受信しました")
        finally:
            self._running_flag[0] = False
            await self.cleanup()
        
    async def _synthesize_with_engine_check(self, text: str) -> Optional[bytes]:
        """Synthesize audio with pending engine switch check
        
        Args:
            text: Text to synthesize
            
        Returns:
            Audio data or None
        """
        print(f"[VoiceChatMode] _synthesize_with_engine_check called: text='{text}', pending_engine={self._pending_engine_switch}")
        
        # Check if engine switch is pending
        if self._pending_engine_switch:
            print(f"[VoiceChatMode] エンジン切り替えが保留中: {self._pending_engine_switch}")
            # Check if the engine is already registered
            if self._pending_engine_switch in self.tts_manager.engines:
                # Just switch to existing engine
                print(f"[VoiceChatMode] 既存のエンジンに切り替え: {self._pending_engine_switch}")
                self.tts_manager.set_engine(self._pending_engine_switch)
            else:
                # Initialize new engine with new character config
                print(f"[VoiceChatMode] 新しいエンジンを初期化: {self._pending_engine_switch}")
                result = await self._initialize_tts_engine(self._pending_engine_switch, self.character_config)
                print(f"[VoiceChatMode] エンジン初期化結果: {result}")
            
            # Clear pending flag
            self._pending_engine_switch = None
        
        # Synthesize audio
        try:
            print(f"[VoiceChatMode] 音声合成を実行: engine={self.tts_manager.current_engine}, character={self.character_name}")
            audio_data = await self.tts_manager.synthesize(
                text,
                character_name=self.character_name
            )
            if audio_data:
                print(f"[VoiceChatMode] 音声合成成功: {len(audio_data)} bytes")
            else:
                print("[VoiceChatMode] 音声合成結果がNoneです")
            return audio_data
        except Exception as e:
            print(f"[VoiceChatMode] 音声合成エラー: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def _play_greeting(self, greeting: str):
        """Play greeting audio"""
        greeting_audio = await self._synthesize_with_engine_check(greeting)
        if greeting_audio:
            self.voice_handler.is_speaking = True
            self.voice_handler.playback_started_time = time.time()
            self.player.play(greeting_audio)
            self.voice_handler.is_speaking = False
            self.voice_handler.playback_finished_time = time.time()
    
    async def _handle_voice_input(self, text: str, audio_type: str):
        """Handle voice input from voice handler"""
        # 音声合成のみの場合（キャラクター切り替え時のgoodbye/greeting）
        if audio_type == 'voice_synthesis':
            # 辞書形式の場合（キーワード応答）
            if isinstance(text, dict) and text.get('type') == 'keyword_response':
                user_text = text.get('user_text', '')
                assistant_text = text.get('assistant_text', '')
                
                # ユーザー入力をWebUIに表示
                if user_text and self.web_interface:
                    self.web_interface.add_user_message(user_text)
                
                # アシスタントの応答を処理
                if assistant_text and self.tts_manager and self.player:
                    print(f"{self.character_name}: {assistant_text}")
                    
                    # WebUIにもメッセージを送信
                    if self.web_interface:
                        self.web_interface.add_assistant_message(assistant_text)
                    
                    # 音声合成
                    audio_data = await self._synthesize_with_engine_check(assistant_text)
                    
                    if audio_data:
                        # 音声再生
                        self.player.play(audio_data)
            # 文字列の場合（従来の処理）
            elif isinstance(text, str) and text:
                if self.tts_manager and self.player:
                    print(f"{self.character_name}: {text}")
                    
                    # WebUIにもメッセージを送信
                    if self.web_interface:
                        self.web_interface.add_assistant_message(text)
                    
                    # 音声合成
                    audio_data = await self._synthesize_with_engine_check(text)
                    
                    if audio_data:
                        # 音声再生
                        self.player.play(audio_data)
            return
        
        # 通常の音声入力処理
        # Voice input should be displayed in UI even if transcription_callback was not called
        # This ensures voice input is always logged properly
        if isinstance(text, str) and text and self.web_interface:
            # Add user message to WebUI if not already added by transcription_callback
            # The web interface should handle duplicate prevention internally
            self.web_interface.add_user_message(text)
        
        # Process with response handler and get the response
        response = await self.response_handler.handle_new_input(text, audio_type)
        
        # Send response to web interface
        if response and self.web_interface:
            self.web_interface.add_assistant_message(response)
    
    async def _process_user_message_web(self, message: str, image_data=None, session_id=None):
        """Process user message from web interface
        
        Args:
            message: User's message text
            image_data: Optional image data dict with 'data', 'mimeType', 'name' keys  
            session_id: Optional conversation session ID from frontend
        """
        try:
            # Check for keywords using universal keyword detection system
            try:
                from src.tools.keyword import process_keywords, get_keyword_manager
                
                # 選択モードの状態を事前確認（デバッグ用）
                manager = get_keyword_manager()
                char_detector = manager.get_detector('character_switch')
                if char_detector and char_detector.is_in_selection_mode():
                    print(f"[WebUI] キーワード処理前: 選択モード中です (テキスト: '{message}')")
                
                keyword_result = process_keywords(message)
                if keyword_result and keyword_result.detected:
                    # メッセージが辞書形式の場合（キャラクター切り替え）
                    if isinstance(keyword_result.message, dict):
                        msg_data = keyword_result.message
                        mode = msg_data.get('mode', '')
                        
                        # 選択モードに入る時
                        if mode == 'selection_mode' and 'goodbye_reply' in msg_data:
                            # CharacterSwitchDetectorの状態を確認
                            manager = get_keyword_manager()
                            char_detector = manager.get_detector('character_switch')
                            if char_detector:
                                print(f"[WebUI] 選択モードに入ります。検出器の選択モード状態: {char_detector.is_in_selection_mode()}")
                            
                            # goodbyeReplyをWebUIに表示
                            if self.web_interface:
                                self.web_interface.add_assistant_message(msg_data['goodbye_reply'])
                            
                            # goodbyeReplyを音声で読み上げ
                            if self.tts_manager and self.player:
                                audio_data = await self._synthesize_with_engine_check(msg_data['goodbye_reply'])
                                if audio_data:
                                    self.player.play(audio_data)
                            
                            print(f"[キーワード検出] {msg_data['message']}")
                            return  # LLM処理をスキップ
                        
                        # キャラクター切り替え完了時
                        elif mode == 'character_switched' and 'greeting' in msg_data:
                            # CharacterSwitchDetectorの状態を確認
                            manager = get_keyword_manager()
                            char_detector = manager.get_detector('character_switch')
                            if char_detector:
                                print(f"[WebUI] キャラクター切り替え完了。検出器の選択モード状態: {char_detector.is_in_selection_mode()}")
                            
                            print(f"[キーワード検出] {msg_data['message']}")
                            
                            # greetingをWebUIに表示
                            if self.web_interface:
                                self.web_interface.add_assistant_message(msg_data['greeting'])
                            
                            # greetingを音声で読み上げ
                            if self.tts_manager and self.player:
                                print(f"[VoiceChatMode] グリーティング音声合成開始: '{msg_data['greeting']}'")
                                audio_data = await self._synthesize_with_engine_check(msg_data['greeting'])
                                if audio_data:
                                    print(f"[VoiceChatMode] グリーティング音声再生: {len(audio_data)} bytes")
                                    self.player.play(audio_data)
                                else:
                                    print("[VoiceChatMode] グリーティング音声合成に失敗しました")
                            
                            return  # LLM処理をスキップ
                        
                        else:
                            print(f"[キーワード検出] {msg_data.get('message', '')}")
                            # 選択モード中の「キャラクターが見つかりません」メッセージ
                            if 'message' in msg_data and self.web_interface:
                                self.web_interface.add_assistant_message(msg_data['message'])
                                
                                # 音声でも読み上げ
                                if self.tts_manager and self.player:
                                    audio_data = await self._synthesize_with_engine_check(msg_data['message'])
                                    if audio_data:
                                        self.player.play(audio_data)
                            
                            # 選択モード以外の辞書形式メッセージもLLM処理をスキップ
                            if keyword_result.bypass_llm:
                                return
                    
                    # 通常のメッセージの場合
                    elif keyword_result.message:
                        print(f"[キーワード検出] {keyword_result.message}")
                        if self.web_interface:
                            self.web_interface.add_assistant_message(keyword_result.message)
                        
                        # 音声でも読み上げ
                        if self.tts_manager and self.player:
                            audio_data = await self._synthesize_with_engine_check(keyword_result.message)
                            if audio_data:
                                self.player.play(audio_data)
                    
                    # Skip normal processing if keyword was handled and LLM bypass is requested
                    if keyword_result.bypass_llm:
                        return
                        
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")
                # エラーが発生しても処理を続行
            
            # Set session ID in LLM client for session-specific message storage
            if session_id and hasattr(self.response_handler, 'llm_client'):
                self.response_handler.llm_client.current_session_id = session_id
                print(f"[VoiceChatMode] Set session_id for message storage: {session_id}")
            
            # 通常のLLM処理
            # Generate response immediately for fast UI display
            task_id = self.response_handler._generate_task_id()
            response = await self.response_handler._generate_response_only(task_id, message, "web", image_data=image_data)
            
            # Clear session ID after generating response
            if hasattr(self.response_handler, 'llm_client'):
                self.response_handler.llm_client.current_session_id = None
            
            # Send response to web interface immediately after generation
            if response and self.web_interface:
                self.web_interface.add_assistant_message(response)
            
            # Handle TTS and playback in background with resource locks
            if response and self.response_handler.tts_manager and self.response_handler.player:
                asyncio.create_task(
                    self.response_handler._speak_response_background(task_id, response)
                )
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ メッセージ処理エラー: {e}")
            print(f"📝 詳細: {error_details}")
            try:
                if self.web_interface:
                    self.web_interface.add_assistant_message(f"申し訳ありません。エラーが発生しました: {str(e)}")
            except Exception as web_error:
                print(f"❌ Webインターフェースエラー送信失敗: {web_error}")
    
    async def _cleanup_mode_specific(self):
        """Cleanup voice chat mode specific resources"""
        # Stop voice handler
        self._running_flag[0] = False
        
        # Stop recording
        if self.voice_handler:
            self.voice_handler.stop_recording()
        
        # Stop any ongoing playback
        if self.player:
            self.player.stop()
        
        # Cleanup TTS
        if self.tts_manager:
            if hasattr(self.tts_manager, 'cleanup'):
                try:
                    if asyncio.iscoroutinefunction(self.tts_manager.cleanup):
                        await self.tts_manager.cleanup()
                    else:
                        self.tts_manager.cleanup()
                except Exception as e:
                    print(f"TTSクリーンアップエラー: {e}")
        
        # Cleanup web interface
        if self.web_interface:
            try:
                print("[Web] Webインターフェースを終了しました")
            except Exception as e:
                print(f"[Web] Webインターフェース終了エラー: {e}")
