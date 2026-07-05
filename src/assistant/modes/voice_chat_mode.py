"""
Voice chat mode for AoiTalk Voice Assistant Framework
"""

import asyncio
import copy
import inspect
import time
import platform
from typing import Any, Dict, Optional
from ..base import BaseAssistant
from ..voice_handler import VoiceHandler
from ..response_handler import ResponseHandler
from ..chat_turn_persistence import (
    ChatTurnPersistence,
    apply_turn_user_context_to_client,
    restore_turn_user_context_on_client,
)
from ..chat_attachment_utils import (
    build_message_with_attachment_context,
    sanitize_chat_attachments,
)
from ..conversation_title_events import maybe_generate_and_broadcast_session_title
from ...llm.generation_policy import generation_policy_for_profile
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
        self._chat_turn_lock = asyncio.Lock()
        self._chat_turn_persistence: Optional[ChatTurnPersistence] = None
        self._response_model_clients: dict[tuple[str, str], Any] = {}

        # Register TTS character switch callback
        self._register_tts_character_switch_callback()

    def _response_model_identity(
        self, response_model: Optional[Dict[str, str]]
    ) -> Optional[tuple[str, str]]:
        if not isinstance(response_model, dict):
            return None
        provider = str(response_model.get("provider") or "").strip()
        model = str(response_model.get("model") or "").strip()
        if not provider or not model:
            return None
        return provider, model

    def _provider_model_config_keys(self, provider: str) -> tuple[str, ...]:
        return {
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "ollama": ("ollama_model", "ollama.model"),
            "sglang": ("sglang_model", "sglang.model"),
            "openai_compatible_local": ("openai_compatible_local.model",),
        }.get(provider, (f"{provider}.model",))

    def _clone_config_for_response_model(self, provider: str, model: str):
        cloned = copy.copy(self.config)
        cloned.config = copy.deepcopy(self.config.config)
        cloned.set("llm_provider", provider)
        cloned.set("llm_model", model)
        cloned.set("response_model_selection_active", True)
        for key in self._provider_model_config_keys(provider):
            cloned.set(key, model)
        return cloned

    def _active_client_matches_response_model(
        self,
        llm_client: Any,
        provider: str,
        model: str,
    ) -> bool:
        current_provider = str(
            getattr(llm_client, "provider_label", None)
            or self.config.get("llm_provider", "")
        ).strip()
        current_model = str(
            getattr(llm_client, "model_name", None)
            or self.config.get("llm_model", "")
        ).strip()
        return current_provider == provider and current_model == model

    def _get_response_model_client(
        self,
        response_model: Optional[Dict[str, str]],
        base_llm_client: Any,
    ):
        identity = self._response_model_identity(response_model)
        if identity is None:
            return base_llm_client

        provider, model = identity
        if base_llm_client and self._active_client_matches_response_model(
            base_llm_client,
            provider,
            model,
        ):
            return base_llm_client

        cached = self._response_model_clients.get(identity)
        if cached is not None:
            return cached

        from ...llm.manager import create_llm_client

        client = create_llm_client(
            self._clone_config_for_response_model(provider, model)
        )
        personality = self.character_config.get("personality", {})
        system_prompt = personality.get(
            "details",
            "あなたは親切なAIアシスタントです。",
        )
        if hasattr(client, "set_system_prompt"):
            client.set_system_prompt(system_prompt)
        self._response_model_clients[identity] = client
        return client

    def _get_chat_turn_persistence(self, llm_client=None) -> ChatTurnPersistence:
        memory_manager = getattr(llm_client, "memory_manager", None)
        if (
            self._chat_turn_persistence is None
            or (memory_manager is not None and self._chat_turn_persistence.memory_manager is not memory_manager)
        ):
            self._chat_turn_persistence = ChatTurnPersistence(memory_manager)
        return self._chat_turn_persistence

    def _get_chat_turn_metadata(
        self,
        llm_client=None,
        image_data=None,
        attachments=None,
        client_message_id=None,
        include_generation_metrics: bool = False,
        media_recognition_metadata=None,
    ) -> dict:
        metadata = {}
        if llm_client and hasattr(llm_client, "_get_memory_metadata"):
            try:
                metadata.update(llm_client._get_memory_metadata() or {})
            except Exception:
                pass
        if (
            include_generation_metrics
            and llm_client
            and hasattr(llm_client, "get_generation_metadata")
        ):
            try:
                metadata.update(llm_client.get_generation_metadata() or {})
            except Exception:
                pass
        sanitized_attachments = sanitize_chat_attachments(attachments)
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        if sanitized_attachments:
            metadata["attachments"] = sanitized_attachments
        if media_recognition_metadata:
            metadata["media_recognition"] = list(media_recognition_metadata)
        if image_data:
            image_items = []
            if isinstance(image_data, dict) and isinstance(image_data.get("images"), list):
                image_items = [item for item in image_data.get("images") if isinstance(item, dict)]
            elif isinstance(image_data, dict):
                image_items = [image_data]
            first_image = image_items[0] if image_items else {}
            metadata.update(
                {
                    "has_image": True,
                    "image_count": len(image_items),
                    "image_mime_type": first_image.get("mimeType"),
                    "image_name": first_image.get("name"),
                }
            )
        return metadata

    async def _broadcast_conversation_persisted(
        self,
        *,
        session_id: Optional[str],
        role: str,
        message_id: Optional[str] = None,
    ) -> None:
        if not self.web_interface or not session_id:
            return
        broadcaster = getattr(self.web_interface, "broadcast_stream_event", None)
        if not broadcaster:
            return
        result = broadcaster(
            "conversation_persisted",
            {"session_id": session_id, "role": role, "message_id": message_id},
        )
        if inspect.isawaitable(result):
            await result

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
        self.tts_manager = TTSManager(getattr(self.config, "config", None))
        
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
        
        elif preferred_engine == 'irodori_tts':
            print("Irodori-TTSエンジンを初期化中...")
            irodori_engine = await self.tts_manager.create_irodori_tts_engine()
            if irodori_engine:
                self.tts_manager.register_engine("irodori_tts", irodori_engine)
                self.tts_manager.set_engine("irodori_tts")
                engine_initialized = True
                print("Irodori-TTSエンジンの初期化完了")
            else:
                print("Irodori-TTSエンジンの初期化に失敗しました")
                return False

        elif preferred_engine == 'miotts':
            print("MioTTSエンジンを初期化中...")
            miotts_engine = await self.tts_manager.create_miotts_engine()
            if miotts_engine:
                self.tts_manager.register_engine("miotts", miotts_engine)
                self.tts_manager.set_engine("miotts")
                engine_initialized = True
                print("MioTTSエンジンの初期化完了")
            else:
                print("MioTTSエンジンの初期化に失敗しました")
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
            """Callback hook kept for voice handler compatibility."""
            return
                
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
        if isinstance(text, str) and text and self.web_interface:
            dispatch_voice_message = getattr(
                self.web_interface,
                "dispatch_voice_message",
                None,
            )
            if callable(dispatch_voice_message) and dispatch_voice_message(text):
                return

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
    
    async def _process_user_message_web(
        self,
        message: str,
        image_data=None,
        session_id=None,
        project_id=None,
        generation_profile=None,
        include_project_context=False,
        edit_message_id=None,
        response_model=None,
        client_message_id=None,
        attachments=None,
        attachment_context=None,
        skip_user_persistence=False,
        persisted_user_message_id=None,
        agent_run_id=None,
        assistant_sender_type=None,
        assistant_sender_id=None,
        assistant_sender_display_name=None,
        sender_user_id=None,
        sender_display_name=None,
        response_started_at_monotonic=None,
        command_capabilities=None,
        media_recognition_metadata=None,
    ):
        """Process user message from web interface
        
        Args:
            message: User's message text
            image_data: Optional image data dict with 'data', 'mimeType', 'name' keys  
            session_id: Optional conversation session ID from frontend
        """
        try:
            llm_client = (
                self.response_handler.llm_client
                if hasattr(self.response_handler, "llm_client")
                else None
            )
            llm_message = build_message_with_attachment_context(
                message,
                attachment_context,
            )
            chat_persistence = self._get_chat_turn_persistence(llm_client)
            user_message = None
            search_tool_results: list[dict[str, Any]] = []
            if session_id and not skip_user_persistence:
                try:
                    user_message = await chat_persistence.save_user_message(
                        session_id=session_id,
                        content=message,
                        metadata=self._get_chat_turn_metadata(
                            llm_client,
                            image_data,
                            attachments,
                            client_message_id,
                            media_recognition_metadata=media_recognition_metadata,
                        ),
                        branch_from_message_id=edit_message_id,
                        sender_type="user" if sender_user_id else None,
                        sender_id=sender_user_id,
                        sender_display_name=sender_display_name,
                    )
                    await self._broadcast_conversation_persisted(
                        session_id=session_id,
                        role="user",
                        message_id=str(user_message.id) if user_message else None,
                    )
                except Exception as e:
                    print(f"[VoiceChatMode] ユーザーメッセージ保存エラー: {e}")

            async def persist_assistant_reply(reply: Optional[str]) -> None:
                if not reply or not session_id:
                    return
                try:
                    metadata = self._get_chat_turn_metadata(
                        llm_client,
                        include_generation_metrics=True,
                    )
                    if isinstance(response_started_at_monotonic, (int, float)):
                        elapsed_ms = int(
                            max(
                                0,
                                round(
                                    (
                                        time.monotonic()
                                        - response_started_at_monotonic
                                    )
                                    * 1000
                                ),
                            )
                        )
                        metadata["response_elapsed_ms"] = elapsed_ms
                    if agent_run_id:
                        metadata["agent_run_id"] = agent_run_id
                    if search_tool_results:
                        metadata["tool_results"] = list(search_tool_results)
                    assistant_message = await chat_persistence.save_assistant_message(
                        session_id=session_id,
                        content=reply,
                        metadata=metadata,
                        sender_type=assistant_sender_type,
                        sender_id=assistant_sender_id,
                        sender_display_name=assistant_sender_display_name,
                    )
                    await self._broadcast_conversation_persisted(
                        session_id=session_id,
                        role="assistant",
                        message_id=(
                            str(assistant_message.id) if assistant_message else None
                        ),
                    )
                    await maybe_generate_and_broadcast_session_title(
                        web_interface=self.web_interface,
                        session_id=session_id,
                        chat_persistence=chat_persistence,
                        config=self.config,
                        log_prefix="VoiceChatMode",
                    )
                except Exception as e:
                    print(f"[VoiceChatMode] アシスタントメッセージ保存エラー: {e}")

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
                                self.web_interface.add_assistant_message(
                                    msg_data['goodbye_reply'], session_id=session_id
                                )
                            await persist_assistant_reply(msg_data['goodbye_reply'])
                            
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
                                self.web_interface.add_assistant_message(
                                    msg_data['greeting'], session_id=session_id
                                )
                            await persist_assistant_reply(msg_data['greeting'])
                            
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
                                self.web_interface.add_assistant_message(
                                    msg_data['message'], session_id=session_id
                                )
                                await persist_assistant_reply(msg_data['message'])
                                
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
                            self.web_interface.add_assistant_message(
                                keyword_result.message, session_id=session_id
                            )
                        await persist_assistant_reply(keyword_result.message)
                        
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
            
            async with self._chat_turn_lock:
                base_llm_client = (
                    self.response_handler.llm_client
                    if hasattr(self.response_handler, "llm_client")
                    else None
                )
                llm_client = self._get_response_model_client(
                    response_model,
                    base_llm_client,
                )
                original_handler_client = getattr(
                    self.response_handler,
                    "llm_client",
                    None,
                )
                if self.response_handler:
                    self.response_handler.llm_client = llm_client
                turn_user_context_snapshot = None
                if llm_client and session_id:
                    exclude_message_id = (
                        str(user_message.id)
                        if user_message
                        else persisted_user_message_id
                    )
                    prompt_history = await chat_persistence.load_prompt_history(
                        session_id=session_id,
                        exclude_message_id=exclude_message_id,
                    )
                    chat_persistence.apply_prompt_history_to_client(
                        llm_client,
                        session_id=session_id,
                        prompt_history=prompt_history,
                    )

                # Set session ID and project ID in LLM client
                if llm_client:
                    turn_user_context_snapshot = apply_turn_user_context_to_client(
                        llm_client,
                        sender_user_id=sender_user_id,
                        sender_display_name=sender_display_name,
                    )
                    if session_id:
                        llm_client.current_session_id = session_id
                    if project_id:
                        llm_client.current_project_id = project_id
                        print(f"[VoiceChatMode] Set project_id: {project_id}")
                    llm_client.generation_policy = generation_policy_for_profile(
                        generation_profile
                    )
                    llm_client.current_include_project_context = bool(include_project_context)
                    llm_client.current_edit_message_id = edit_message_id
                    llm_client.current_response_model = response_model
                    llm_client.external_persistence_enabled = bool(
                        user_message or (skip_user_persistence and session_id)
                    )

                # ストリーミングコールバック: WebSocket経由でフロントエンドにイベントを配信
                stream_callback = None
                steering_callback = None
                used_streaming = False
                supports_streaming = bool(
                    llm_client and hasattr(llm_client, '_run_streamed_with_callback')
                )
                if session_id and self.web_interface and hasattr(self.web_interface, 'consume_generation_steering'):
                    web_iface = self.web_interface
                    async def _steering_callback():
                        result = web_iface.consume_generation_steering(session_id)
                        if inspect.isawaitable(result):
                            result = await result
                        return result or []
                    steering_callback = _steering_callback

                if supports_streaming and self.web_interface and hasattr(self.web_interface, 'broadcast_stream_event'):
                    web_iface = self.web_interface
                    async def _stream_callback(event_type: str, data: dict):
                        nonlocal used_streaming
                        used_streaming = True
                        try:
                            event_data = dict(data)
                            if session_id:
                                event_data["session_id"] = session_id
                            if agent_run_id:
                                event_data["agent_run_id"] = agent_run_id
                            tool_result = event_data.get("tool_result")
                            if (
                                event_type == "tool_end"
                                and isinstance(tool_result, dict)
                            ):
                                search_tool_results.append(tool_result)
                            result = web_iface.broadcast_stream_event(event_type, event_data)
                            if inspect.isawaitable(result):
                                await result
                        except Exception as e:
                            print(f"[VoiceChatMode] ストリーミングイベント送信エラー: {e}")
                    stream_callback = _stream_callback

                # 通常のLLM処理
                task_id = self.response_handler._generate_task_id()
                try:
                    generation_kwargs = {
                        "image_data": image_data,
                        "stream_callback": stream_callback,
                    }
                    try:
                        generation_signature = inspect.signature(
                            self.response_handler._generate_response_only
                        )
                        if "steering_callback" in generation_signature.parameters:
                            generation_kwargs["steering_callback"] = steering_callback
                    except (TypeError, ValueError):
                        pass
                    response = await self.response_handler._generate_response_only(
                        task_id,
                        llm_message,
                        "web",
                        **generation_kwargs,
                    )
                finally:
                    if self.response_handler:
                        self.response_handler.llm_client = original_handler_client
                    if llm_client:
                        llm_client.current_session_id = None
                        llm_client.current_project_id = None
                        llm_client.generation_policy = generation_policy_for_profile(None)
                        llm_client.current_include_project_context = None
                        llm_client.current_edit_message_id = None
                        llm_client.current_response_model = None
                        llm_client.external_persistence_enabled = False
                        restore_turn_user_context_on_client(
                            llm_client,
                            turn_user_context_snapshot,
                        )

                if response:
                    await persist_assistant_reply(response)

                # ストリーミング未使用時のみnew_messageで応答を送信
                if response and self.web_interface and not used_streaming:
                    self.web_interface.add_assistant_message(response, session_id=session_id)
                elif not response:
                    failure_reply = (
                        "応答生成に失敗しました。LLMサーバーまたはモデル設定を確認してください。"
                    )
                    await persist_assistant_reply(failure_reply)
                    if self.web_interface:
                        self.web_interface.add_assistant_message(
                            failure_reply, session_id=session_id
                        )
            
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
            error_reply = f"申し訳ありません。応答生成中にエラーが発生しました: {e}"
            if session_id:
                try:
                    llm_client = (
                        self.response_handler.llm_client
                        if hasattr(self.response_handler, "llm_client")
                        else None
                    )
                    chat_persistence = self._get_chat_turn_persistence(llm_client)
                    assistant_message = await chat_persistence.save_assistant_message(
                        session_id=session_id,
                        content=error_reply,
                        metadata=self._get_chat_turn_metadata(llm_client),
                    )
                    await self._broadcast_conversation_persisted(
                        session_id=session_id,
                        role="assistant",
                        message_id=(
                            str(assistant_message.id) if assistant_message else None
                        ),
                    )
                except Exception as persist_error:
                    print(
                        f"[VoiceChatMode] エラー応答の保存に失敗しました: {persist_error}"
                    )
            try:
                if self.web_interface:
                    self.web_interface.add_assistant_message(
                        error_reply,
                        session_id=session_id,
                    )
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
