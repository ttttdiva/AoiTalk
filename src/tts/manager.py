"""
TTS engine manager
"""
import asyncio
import platform
import re
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod

# Cross-platform TTS engines (work on all platforms)
from .engines.voicevox_engine import VoicevoxEngine
from .engines.aivisspeech_engine import AivisSpeechEngine
from .engines.nijivoice_engine import NijivoiceEngine
from .engines.qwen3_tts_engine import Qwen3TTSEngine

# Windows-only TTS engines (require pythonnet, pywin32, etc.)
# These are conditionally imported to allow running on Linux/Docker
_WINDOWS_ENGINES_AVAILABLE = platform.system() == "Windows"
VoiceroidEngine = None
AIVoiceEngine = None
CevioEngine = None
resolve_voiceroid_voice_id = None

if _WINDOWS_ENGINES_AVAILABLE:
    try:
        from .engines.voiceroid_engine import VoiceroidEngine, resolve_voiceroid_voice_id
    except ImportError as e:
        print(f"[TTSManager] VOICEROID engine not available: {e}")
        VoiceroidEngine = None
        resolve_voiceroid_voice_id = None
    
    try:
        from .engines.aivoice_engine import AIVoiceEngine
    except ImportError as e:
        print(f"[TTSManager] A.I.VOICE engine not available: {e}")
        AIVoiceEngine = None
    
    try:
        from .engines.cevio_engine import CevioEngine
    except ImportError as e:
        print(f"[TTSManager] CeVIO engine not available: {e}")
        CevioEngine = None
else:
    print(f"[TTSManager] Running on {platform.system()} - Windows-only TTS engines disabled")


class TTSEngineBase(ABC):
    """Base class for TTS engines"""
    
    @abstractmethod
    async def synthesize(self, text: str, **kwargs) -> Optional[bytes]:
        """Synthesize speech from text"""
        pass
        

class TTSManager:
    """Manager for Text-to-Speech engines"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize TTS manager"""
        self.engines: Dict[str, TTSEngineBase] = {}
        self.current_engine = None
        self.character_configs: Dict[str, Dict[str, Any]] = {}
        self.config = config or {}
        
    def _preprocess_text(self, text: str) -> str:
        """Preprocess text to remove URLs before TTS
        
        Args:
            text: Input text
            
        Returns:
            Text with URLs removed
        """
        # URL pattern matching http/https URLs
        url_pattern = r'https?://[^\s]+'
        
        # Remove URLs and replace with empty string
        processed_text = re.sub(url_pattern, '', text)
        
        # Clean up extra whitespace
        processed_text = re.sub(r'\s+', ' ', processed_text).strip()
        
        return processed_text
        
    def register_engine(self, name: str, engine: TTSEngineBase):
        """Register a TTS engine
        
        Args:
            name: Engine name
            engine: TTS engine instance
        """
        self.engines[name] = engine
        
    def set_engine(self, name: str) -> bool:
        """Set current TTS engine
        
        Args:
            name: Engine name
            
        Returns:
            True if engine set successfully
        """
        if name in self.engines:
            self.current_engine = name
            print(f"[TTSManager] エンジンを切り替えました: {name}")
            return True
        else:
            print(f"[TTSManager] エンジンが見つかりません: {name}")
            print(f"[TTSManager] 登録済みエンジン: {list(self.engines.keys())}")
            return False
        
    def register_character(self, character_name: str, config: Dict[str, Any]):
        """Register character configuration
        
        Args:
            character_name: Character name
            config: Character configuration
        """
        self.character_configs[character_name] = config

    def _resolve_voiceroid_voice(self, char_config: Dict[str, Any], kwargs: Dict[str, Any], speed_adjustment: float):
        """Compute VOICEROID voice selection and parameters"""
        if resolve_voiceroid_voice_id is None:
            return {'voice_index': None, 'voice_name': None, 'voice_id': None, 'params': {}, 'speed': 1.0}
            
        voice_config = char_config.get('voice', {})
        params = voice_config.get('parameters', {})

        voice_name = voice_config.get('voice_name')
        voice_id = voice_config.get('voice_id')

        voice_index = voice_config.get('voice_index')
        if voice_config.get('speaker_id') is not None:
            voice_index = voice_config['speaker_id']

        if not voice_id:
            alias_candidates = []
            if voice_name:
                alias_candidates.append(voice_name)
            char_display_name = char_config.get('name')
            if char_display_name:
                alias_candidates.append(char_display_name)

            for candidate in alias_candidates:
                resolved_id = resolve_voiceroid_voice_id(candidate)
                if resolved_id:
                    voice_id = resolved_id
                    break

        effective_voice_index = voice_index if voice_index is not None else kwargs.get('voice_index')
        effective_voice_name = voice_name if voice_name is not None else kwargs.get('voice_name')
        effective_voice_id = voice_id if voice_id is not None else kwargs.get('voice_id')

        base_speed = params.get('speed', kwargs.get('speed', 1.35))
        adjusted_speed = base_speed * speed_adjustment
        adjusted_speed = max(0.5, min(4.0, adjusted_speed))

        selection = {
            'voice_index': effective_voice_index,
            'voice_name': effective_voice_name,
            'voice_id': effective_voice_id,
            'params': params,
            'speed': adjusted_speed
        }
        return selection

    def prepare_character_voice(self, character_name: str):
        """Force engine to preload the appropriate voice for the character"""
        if self.current_engine != 'voiceroid':
            return
        if VoiceroidEngine is None:
            return
        engine = self.engines.get('voiceroid')
        if not engine or character_name not in self.character_configs:
            return

        selection = self._resolve_voiceroid_voice(self.character_configs[character_name], {}, 1.0)
        effective_voice_index = selection['voice_index']
        effective_voice_id = selection['voice_id']
        effective_voice_name = selection['voice_name']

        target_index = None
        voices = engine.get_voices()
        if effective_voice_id and effective_voice_id in voices:
            target_index = voices.index(effective_voice_id)
        elif effective_voice_name and effective_voice_name in voices:
            target_index = voices.index(effective_voice_name)
        elif isinstance(effective_voice_index, int) and 0 <= effective_voice_index < len(voices):
            target_index = effective_voice_index

        if target_index is not None:
            if engine.current_voice_index != target_index:
                print(
                    f"[TTSManager][VOICEROID] preload voice -> name={effective_voice_name}, "
                    f"id={effective_voice_id}, index={target_index}"
                )
                engine.load_voice_directly(target_index)
        else:
            print(
                f"[TTSManager][VOICEROID] Could not preload voice for '{character_name}'. "
                f"Resolved name={effective_voice_name}, id={effective_voice_id}, index={effective_voice_index}"
            )
        
    async def create_voicevox_engine(self, engine_path: str) -> Optional[VoicevoxEngine]:
        """Create and initialize VOICEVOX engine
        
        Args:
            engine_path: Path to VOICEVOX engine
            
        Returns:
            Initialized VoicevoxEngine or None
        """
        engine = VoicevoxEngine(engine_path)
        
        # Start engine process
        if not engine.start_engine():
            return None
            
        # Initialize client
        if not await engine.initialize():
            engine.stop_engine()
            return None
            
        return engine
        
    async def create_voiceroid_engine(self, character_config: Optional[dict] = None) -> Optional["VoiceroidEngine"]:
        """Create and initialize VOICEROID engine
        
        Args:
            character_config: Character configuration for initial voice setup
        
        Returns:
            Initialized VoiceroidEngine or None
        """
        if VoiceroidEngine is None:
            print("[TTSManager] VOICEROID engine is not available on this platform")
            return None
            
        engine = VoiceroidEngine()
        
        # Initialize engine
        if not await engine.initialize():
            return None
        
        # Set initial voice if character config provided (sample.py style)
        if character_config:
            voice_config = character_config.get('voice', {})
            initial_voice_index = None
            
            # Determine initial voice index
            if 'voice_name' in voice_config:
                # Find index by name
                voice_name = voice_config['voice_name']
                available_voices = engine.get_voices()
                if voice_name in available_voices:
                    initial_voice_index = available_voices.index(voice_name)
            elif 'speaker_id' in voice_config:
                initial_voice_index = voice_config['speaker_id']
            elif 'voice_index' in voice_config:
                initial_voice_index = voice_config['voice_index']
            
            # Set initial voice using sample.py method
            if initial_voice_index is not None:
                engine.load_voice_directly(initial_voice_index)
            
        return engine
        
    async def create_aivoice_engine(self, aivoice_path: Optional[str] = None) -> Optional["AIVoiceEngine"]:
        """Create and initialize A.I.VOICE engine
        
        Args:
            aivoice_path: Path to A.I.VOICE executable (optional)
        
        Returns:
            Initialized AIVoiceEngine or None
        """
        if AIVoiceEngine is None:
            print("[TTSManager] A.I.VOICE engine is not available on this platform")
            return None
            
        engine = AIVoiceEngine(aivoice_path) if aivoice_path else AIVoiceEngine()
        
        # Initialize engine
        if not await engine.initialize():
            return None
            
        return engine
        
    async def create_cevio_engine(self) -> Optional["CevioEngine"]:
        """Create and initialize CeVIO AI engine
        
        Returns:
            Initialized CevioEngine or None
        """
        if CevioEngine is None:
            print("[TTSManager] CeVIO engine is not available on this platform")
            return None
            
        engine = CevioEngine()
        
        # Initialize engine
        if not await engine.initialize():
            return None
            
        return engine
        
    async def create_aivisspeech_engine(self, engine_path: str) -> Optional[AivisSpeechEngine]:
        """Create and initialize AivisSpeech engine
        
        Args:
            engine_path: Path to AivisSpeech engine
            
        Returns:
            Initialized AivisSpeechEngine or None
        """
        # Get AivisSpeech specific settings from config
        aivisspeech_settings = self.config.get('tts_settings', {}).get('aivisspeech', {})
        host = aivisspeech_settings.get('host', '127.0.0.1')
        port = aivisspeech_settings.get('port', 10101)
        use_gpu = aivisspeech_settings.get('use_gpu', False)
        
        engine = AivisSpeechEngine(engine_path, host=host, port=port, use_gpu=use_gpu)
        
        # Start engine process
        if not engine.start_engine():
            return None
            
        # Initialize client
        if not await engine.initialize():
            engine.stop_engine()
            return None
        
        # Get available speakers for debugging (non-blocking)
        try:
            print(f"[TTSManager] スピーカー情報を取得中...")
            # Create a task with timeout to avoid blocking
            import asyncio
            speakers_task = asyncio.create_task(engine.get_speakers())
            try:
                speakers = await asyncio.wait_for(speakers_task, timeout=5.0)
                if speakers:
                    print(f"[TTSManager] AivisSpeech initialized with {len(speakers)} speakers")
                else:
                    print(f"[TTSManager] スピーカー情報の取得に失敗しました")
            except asyncio.TimeoutError:
                print(f"[TTSManager] スピーカー情報取得がタイムアウトしました")
                speakers_task.cancel()
        except Exception as e:
            print(f"[TTSManager] スピーカー情報取得エラー: {e}")
            
        return engine
        
    async def create_nijivoice_engine(self, api_key: Optional[str] = None) -> Optional[NijivoiceEngine]:
        """Create and initialize Nijivoice engine
        
        Args:
            api_key: API key for Nijivoice service
            
        Returns:
            Initialized NijivoiceEngine or None
        """
        engine = NijivoiceEngine(api_key=api_key)
        
        # Initialize engine
        if not await engine.initialize():
            return None
            
        return engine
        
    async def create_qwen3_tts_engine(
        self,
        model_name: Optional[str] = None,
        cache_dir: Optional[str] = None,
        voices_dir: Optional[str] = None,
        use_gpu: Optional[bool] = None,
    ) -> Optional[Qwen3TTSEngine]:
        """Create and initialize Qwen3-TTS engine
        
        Args:
            model_name: HuggingFace model name or local path
            cache_dir: Directory to cache the model
            voices_dir: Directory to store voice embeddings
            use_gpu: Whether to use GPU acceleration
            
        Returns:
            Initialized Qwen3TTSEngine or None
        """
        # Get Qwen3-TTS settings from config
        qwen3_settings = self.config.get('tts_settings', {}).get('qwen3tts', {})
        
        # Use provided values or fall back to config
        model_name = model_name or qwen3_settings.get('model_name', 'Qwen/Qwen3-TTS-12Hz-1.7B-Base')
        cache_dir = cache_dir or qwen3_settings.get('cache_dir', 'cache/qwen3_models')
        voices_dir = voices_dir or qwen3_settings.get('voices_dir', 'cache/qwen3_voices')
        use_gpu = use_gpu if use_gpu is not None else qwen3_settings.get('use_gpu', True)
        
        # Pass Config object to engine (for accessing existing Gemini settings)
        engine = Qwen3TTSEngine(
            model_name=model_name,
            cache_dir=cache_dir,
            voices_dir=voices_dir,
            use_gpu=use_gpu,
            config=self.config,
        )
        
        # Initialize engine
        if not await engine.initialize():
            return None
        
        return engine
        
    async def synthesize(self, 
                        text: str,
                        character_name: Optional[str] = None,
                        **kwargs) -> Optional[bytes]:
        """Synthesize speech using current engine
        
        Args:
            text: Text to synthesize
            character_name: Character name for voice parameters
            **kwargs: Additional parameters for the engine
            
        Returns:
            WAV audio data as bytes
        """
        if not self.current_engine or self.current_engine not in self.engines:
            print("[TTSManager] No TTS engine available")
            print(f"[TTSManager] Current engine: {self.current_engine}, Available engines: {list(self.engines.keys())}")
            return None
        
        print(f"[TTSManager] 音声合成開始 - エンジン: {self.current_engine}, キャラクター: {character_name}, テキスト長: {len(text)}")
            
        # Preprocess text to remove URLs
        processed_text = self._preprocess_text(text)
        if not processed_text:
            print("[TTSManager] Text is empty after preprocessing")
            return None
            
        engine = self.engines[self.current_engine]
        
        # Get global speed adjustment from config (reload config to get latest value)
        import yaml
        import os
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../config/config.yaml')
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                current_config = yaml.safe_load(f)
            speed_adjustment = current_config.get('tts', {}).get('speed_adjustment', 1.0)
        except:
            speed_adjustment = self.config.get('tts', {}).get('speed_adjustment', 1.0)
        
        # Get character configuration if specified
        if character_name and character_name in self.character_configs:
            char_config = self.character_configs[character_name]
            
            # Extract voice parameters based on engine type
            if self.current_engine == "voicevox" and isinstance(engine, VoicevoxEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Check if this character is meant for a different engine
                intended_engine = voice_config.get('engine', 'voicevox')
                if intended_engine != 'voicevox':
                    # Fallback to default VOICEVOX character when using non-VOICEVOX character
                    print(f"[TTSManager] キャラクター'{character_name}'は{intended_engine}エンジン用です。VOICEVOXのデフォルトキャラクターにフォールバックします。")
                    speaker_id = 3  # Default to ずんだもん
                else:
                    speaker_id = voice_config.get('speaker_id', kwargs.get('speaker_id', 3))
                
                # Override with character-specific parameters
                base_speed = params.get('speed', kwargs.get('speed', 1.0))
                adjusted_speed = base_speed * speed_adjustment
                # VOICEVOX speed range: 0.5-2.0
                adjusted_speed = max(0.5, min(2.0, adjusted_speed))
                
                kwargs.update({
                    'speaker_id': speaker_id,
                    'speed': adjusted_speed,
                    'pitch': params.get('pitch', kwargs.get('pitch', 0.0)),
                    'intonation': params.get('intonation', kwargs.get('intonation', 1.0)),
                    'volume': params.get('volume', kwargs.get('volume', 1.0))
                })
                
            elif self.current_engine == "voiceroid" and VoiceroidEngine is not None and isinstance(engine, VoiceroidEngine):
                selection = self._resolve_voiceroid_voice(char_config, kwargs, speed_adjustment)

                print(
                    "[TTSManager][VOICEROID] voice selection -> "
                    f"name={selection['voice_name']}, id={selection['voice_id']}, index={selection['voice_index']}"
                )

                params = selection['params'] or {}

                # ランタイム話速（セッション/GUI）をキャラクター基準値に乗算
                base_speed = selection['speed']
                speed_multiplier = kwargs.get('speed')
                if speed_multiplier is None:
                    speed_multiplier = 1.0
                runtime_speed = base_speed * speed_multiplier

                # ピッチはキャラクター基準値に対するオフセットとして扱う
                base_pitch = params.get('pitch')
                pitch_offset = kwargs.get('pitch')
                if pitch_offset is None:
                    pitch_offset = 0.0
                if base_pitch is None:
                    if pitch_offset != 0.0:
                        runtime_pitch = pitch_offset
                        base_pitch = pitch_offset
                        applied_pitch_delta = 0.0
                    else:
                        base_pitch = 1.1
                        runtime_pitch = 1.1
                        applied_pitch_delta = 0.0
                else:
                    runtime_pitch = base_pitch + pitch_offset
                    applied_pitch_delta = pitch_offset

                print(
                    "[TTSManager][VOICEROID] apply params -> "
                    f"speed={runtime_speed:.2f} (base={base_speed:.2f}, x{speed_multiplier:.2f}), "
                    f"pitch={runtime_pitch:.2f} (base={base_pitch:.2f}, +{applied_pitch_delta:.2f})"
                )

                kwargs.update({
                    'voice_index': selection['voice_index'],
                    'voice_name': selection['voice_name'],
                    'voice_id': selection['voice_id'],
                    'volume': params.get('volume', kwargs.get('volume', 1.9)),
                    'speed': runtime_speed,
                    'pitch': runtime_pitch,
                    'emphasis': params.get('emphasis', kwargs.get('emphasis', 1.0)),
                    'pause_middle': params.get('pause_middle', kwargs.get('pause_middle', 150)),
                    'pause_long': params.get('pause_long', kwargs.get('pause_long', 370)),
                    'pause_sentence': params.get('pause_sentence', kwargs.get('pause_sentence', 800)),
                    'master_volume': params.get('master_volume', kwargs.get('master_volume', 1.0))
                })

            elif self.current_engine == "aivoice" and AIVoiceEngine is not None and isinstance(engine, AIVoiceEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Handle voice selection - support both speaker_id and voice_index for compatibility
                voice_index = None
                voice_name = None
                
                # Priority: voice_name > speaker_id > voice_index
                if 'voice_name' in voice_config:
                    voice_name = voice_config['voice_name']
                elif 'speaker_id' in voice_config:
                    voice_index = voice_config['speaker_id']  # Map speaker_id to voice_index
                elif 'voice_index' in voice_config:
                    voice_index = voice_config['voice_index']
                
                # Override with character-specific parameters
                base_speed = params.get('speed', kwargs.get('speed', 1.0))
                adjusted_speed = base_speed * speed_adjustment
                # A.I.VOICE speed range: assume 0.5-2.0 (similar to VOICEVOX)
                adjusted_speed = max(0.5, min(2.0, adjusted_speed))
                
                kwargs.update({
                    'voice_index': voice_index if voice_index is not None else kwargs.get('voice_index'),
                    'voice_name': voice_name if voice_name is not None else kwargs.get('voice_name'),
                    'speed': adjusted_speed,
                    'pitch': params.get('pitch', kwargs.get('pitch', 1.0)),
                    'volume': params.get('volume', kwargs.get('volume', 1.0)),
                    'intonation': params.get('intonation', kwargs.get('intonation', 1.0))
                })
                
            elif self.current_engine == "cevio" and CevioEngine is not None and isinstance(engine, CevioEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Handle voice selection - support both speaker_id and voice_index for compatibility
                voice_index = None
                voice_name = None
                
                # Priority: voice_name > speaker_id > voice_index
                if 'voice_name' in voice_config:
                    voice_name = voice_config['voice_name']
                elif 'speaker_id' in voice_config:
                    voice_index = voice_config['speaker_id']  # Map speaker_id to voice_index
                elif 'voice_index' in voice_config:
                    voice_index = voice_config['voice_index']
                
                # Override with character-specific parameters
                # CeVIO uses 'rate' instead of 'speed', convert from speed multiplier to rate offset
                base_rate = params.get('rate', kwargs.get('rate', 0))
                # Convert speed adjustment to rate change: speed 1.2 -> rate +2, speed 0.8 -> rate -2
                rate_adjustment = (speed_adjustment - 1.0) * 10
                adjusted_rate = base_rate + rate_adjustment
                # CeVIO rate range: -10 to 10
                adjusted_rate = max(-10, min(10, adjusted_rate))
                
                kwargs.update({
                    'voice_index': voice_index if voice_index is not None else kwargs.get('voice_index'),
                    'voice_name': voice_name if voice_name is not None else kwargs.get('voice_name'),
                    'rate': int(adjusted_rate),        # -10 to 10
                    'volume': params.get('volume', kwargs.get('volume', 100))  # 0 to 100
                })
                
            elif self.current_engine == "aivisspeech" and isinstance(engine, AivisSpeechEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Override with character-specific parameters
                base_speed = params.get('speed', kwargs.get('speed', 1.0))
                adjusted_speed = base_speed * speed_adjustment
                # AivisSpeech speed range: 0.5-2.0 (VOICEVOX-compatible)
                adjusted_speed = max(0.5, min(2.0, adjusted_speed))
                
                kwargs.update({
                    'speaker_id': voice_config.get('speaker_id', kwargs.get('speaker_id', 0)),
                    'speed': adjusted_speed,
                    'pitch': params.get('pitch', kwargs.get('pitch', 0.0)),
                    'intonation': params.get('intonation', kwargs.get('intonation', 1.0)),
                    'volume': params.get('volume', kwargs.get('volume', 1.0))
                })
                
            elif self.current_engine == "nijivoice" and isinstance(engine, NijivoiceEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Handle voice selection
                voice_id = None
                voice_name = None
                
                # Priority: voice_id > voice_name > speaker_id
                if 'voice_id' in voice_config:
                    voice_id = voice_config['voice_id']
                elif 'voice_name' in voice_config:
                    voice_name = voice_config['voice_name']
                elif 'speaker_id' in voice_config:
                    voice_id = voice_config['speaker_id']  # Map speaker_id to voice_id for compatibility
                
                # Override with character-specific parameters
                base_speed = params.get('speed', kwargs.get('speed', 1.0))
                adjusted_speed = base_speed * speed_adjustment
                # Nijivoice speed range: assume 0.5-2.0
                adjusted_speed = max(0.5, min(2.0, adjusted_speed))
                
                kwargs.update({
                    'voice_id': voice_id if voice_id is not None else kwargs.get('voice_id'),
                    'voice_name': voice_name if voice_name is not None else kwargs.get('voice_name'),
                    'speed': adjusted_speed,
                    'emotionalLevel': params.get('emotionalLevel', kwargs.get('emotionalLevel', 0.1)),
                    'soundDuration': params.get('soundDuration', kwargs.get('soundDuration', 0.1)),
                    'format': params.get('format', kwargs.get('format', 'mp3'))
                })
                
            elif self.current_engine == "qwen3tts" and isinstance(engine, Qwen3TTSEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                
                # Handle voice selection
                voice_name = voice_config.get('voice_name', kwargs.get('voice_name'))
                language = voice_config.get('language', kwargs.get('language', 'Auto'))
                
                # Note: Qwen3-TTS generates at a fixed rate, so we'll pass these
                # as generation kwargs instead
                kwargs.update({
                    'voice_name': voice_name,
                    'character_name': character_name,  # Pass character_name for auto-selection
                    'language': language,
                    'temperature': params.get('temperature', kwargs.get('temperature', 0.9)),
                    'top_p': params.get('top_p', kwargs.get('top_p', 1.0)),
                    'top_k': params.get('top_k', kwargs.get('top_k', 50)),
                })
                
        # Synthesize audio with processed text
        try:
            print(f"[TTSManager] 音声合成を実行中... (エンジン: {self.current_engine})")
            # 直接awaitを使用（create_taskを使わない）
            audio_data = await engine.synthesize(processed_text, **kwargs)
            if audio_data:
                print(f"[TTSManager] 音声合成完了 - サイズ: {len(audio_data)} bytes")
            else:
                print(f"[TTSManager] 音声合成結果がNullです")
            return audio_data
        except Exception as e:
            print(f"[TTSManager] Synthesis error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
        
    async def cleanup(self):
        """Cleanup all engines"""
        for engine in self.engines.values():
            if hasattr(engine, 'stop_engine'):
                engine.stop_engine()
            elif hasattr(engine, 'cleanup'):
                if asyncio.iscoroutinefunction(engine.cleanup):
                    await engine.cleanup()
                else:
                    engine.cleanup() 
