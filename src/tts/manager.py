"""
TTS engine manager
"""
import asyncio
import platform
import re
import threading
import weakref
from collections.abc import Mapping
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod

# Cross-platform TTS engines (work on all platforms)
from .engines.voicevox_engine import VoicevoxEngine
from .engines.aivisspeech_engine import AivisSpeechEngine
from .engines.nijivoice_engine import NijivoiceEngine
from .engines.miotts_engine import MioTTSEngine
from .irodori_config import (
    IRODORI_TTS_CHECKPOINT,
    normalize_irodori_settings,
    resolve_irodori_checkpoint,
)
from .yomi_linter import get_yomi_preflight_service
IrodoriTTSEngine = None

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
        # A character may select a different Irodori checkpoint without
        # replacing the manager's app-wide engine.  Wrappers are cheap and
        # model weights remain governed by the vendored process-global runtime
        # cache, so retain one initialized wrapper per resolved checkpoint.
        self._irodori_engine_cache: Dict[str, Any] = {}
        self.yomi_preflight = get_yomi_preflight_service()
        # Character voice settings are persisted in the ECC database and may
        # be edited while voice chat is running.  Serialize refreshes so a
        # concurrent pair of utterances cannot install an older snapshot after
        # a newer one.  A short lookup timeout keeps DB outages from blocking
        # normal synthesis; callers retain the in-memory snapshot on failure.
        # TTSManager instances are used by both the long-lived voice-chat loop
        # and short-lived FastAPI preview loops.  asyncio primitives must not
        # be shared across those loops, so keep one lock per live loop and let
        # closed loops fall out of the weak-key map.
        self._character_refresh_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()
        self._character_refresh_locks_guard = threading.Lock()
        
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

    @staticmethod
    def _db_character_to_voice_config(
        db_character: Mapping[str, Any],
        existing_config: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        """Convert a Character service row to the manager's YAML shape.

        The database stores voice fields flat (``voice_engine`` and
        ``voice_parameters``), while the legacy manager consumes a nested
        ``voice`` mapping.  Voice parameters are replaced as a whole so a GUI
        clear (for example removing a caption or all reference assets) is
        reflected immediately rather than being merged into stale state.
        """
        base = dict(existing_config) if isinstance(existing_config, Mapping) else {}
        existing_voice = base.get("voice")
        voice = dict(existing_voice) if isinstance(existing_voice, Mapping) else {}

        db_voice = db_character.get("voice")
        if isinstance(db_voice, Mapping):
            # Accept already-normalized service adapters in addition to the
            # canonical flat Character.to_dict() shape.
            voice.update(dict(db_voice))

        voice_parameters = db_character.get("voice_parameters")
        if not isinstance(voice_parameters, Mapping) and isinstance(db_voice, Mapping):
            voice_parameters = db_voice.get("parameters")
        if isinstance(voice_parameters, Mapping):
            parameters = dict(voice_parameters)
        else:
            parameters = {}

        # A few service/test adapters expose these Irodori fields at the row
        # level instead of nesting them under voice_parameters.  Accept both
        # forms without copying secrets or unrelated character fields.
        for key in (
            "caption",
            "irodori_reference_assets",
            "ref_wavs",
            "ref_latents",
            "ref_wav",
            "ref_latent",
            "no_ref",
            "irodori_model",
            "hf_checkpoint",
            "voice_design_checkpoint",
        ):
            value = db_character.get(key)
            if value is not None and key not in parameters:
                parameters[key] = value

        engine_value = (
            db_character.get("voice_engine")
            if "voice_engine" in db_character
            else voice.get("engine", "")
        )
        voice_name_value = (
            db_character.get("voice_name")
            if "voice_name" in db_character
            else voice.get("voice_name", "")
        )
        voice_id_value = (
            db_character.get("voice_id")
            if "voice_id" in db_character
            else voice.get("voice_id", "")
        )
        speaker_value = (
            db_character.get("speaker_id")
            if "speaker_id" in db_character
            else voice.get("speaker_id")
        )
        voice.update(
            {
                "engine": str(engine_value or "").strip(),
                "voice_name": voice_name_value or "",
                "voice_id": voice_id_value or "",
                "speaker_id": speaker_value,
                "parameters": parameters,
            }
        )
        base["name"] = db_character.get("name") or base.get("name") or ""
        base["voice"] = voice
        # Keep the original row available to code that already consumes this
        # private metadata, but do not log or interpolate its values.
        base["_db_character"] = dict(db_character)
        return base

    def _character_refresh_lock_for_current_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._character_refresh_locks_guard:
            lock = self._character_refresh_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._character_refresh_locks[loop] = lock
            return lock

    async def _refresh_character_config_from_db(
        self,
        character_name: Optional[str],
    ) -> None:
        """Serialize refreshes within the current asyncio event loop only."""
        lock = self._character_refresh_lock_for_current_loop()
        async with lock:
            await self._refresh_character_config_from_db_unlocked(character_name)

    async def _refresh_character_config_from_db_unlocked(
        self,
        character_name: Optional[str],
    ) -> None:
        """Refresh one character's voice snapshot before normal synthesis.

        This is deliberately best-effort.  A missing/temporarily unavailable
        database must not prevent voice chat from using the last registered
        configuration.  An explicitly persisted ``irodori_tts`` setting can
        trigger lazy engine creation; other persisted engines switch only when
        already registered by the normal voice-chat lifecycle.
        """
        if not character_name:
            return

        try:
            from ..services.character_service import get_character_for_prompt

            db_character = await asyncio.wait_for(
                get_character_for_prompt(str(character_name)),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            print(
                "[TTSManager] Character voice refresh timed out; "
                "using cached configuration"
            )
            return
        except Exception as exc:
            # Never include the exception text: DBAPI/asyncpg messages may
            # echo a DSN containing credentials.  Type-only diagnostics are
            # sufficient for troubleshooting this best-effort path.
            print(
                "[TTSManager] Character voice refresh unavailable; "
                f"using cached configuration ({type(exc).__name__})"
            )
            return

        if not isinstance(db_character, Mapping):
            # None means a legitimate cache miss from get_character_for_prompt.
            return

        cached = self.character_configs.get(str(character_name))
        updated = self._db_character_to_voice_config(db_character, cached)
        self.character_configs[str(character_name)] = updated

        voice = updated.get("voice")
        preferred_engine = (
            str(voice.get("engine") or "").strip().lower()
            if isinstance(voice, Mapping)
            else ""
        )
        if preferred_engine == "irodori_tts":
            # A GUI can switch a running character to Irodori without a
            # process restart.  Reuse an existing initialized engine whenever
            # possible; otherwise lazy-create it now so this utterance uses
            # the persisted voice settings.
            if "irodori_tts" not in self.engines:
                try:
                    character_settings = self._irodori_settings_from_character(updated)
                    explicit_checkpoint = any(
                        key in character_settings
                        for key in (
                            "irodori_model",
                            "hf_checkpoint",
                            "voice_design_checkpoint",
                        )
                    )
                    if explicit_checkpoint:
                        checkpoint = resolve_irodori_checkpoint(
                            character_settings,
                            fallback_settings=self._irodori_global_settings(),
                        )
                        irodori_engine = await self.create_irodori_tts_engine(
                            hf_checkpoint=checkpoint
                        )
                    else:
                        irodori_engine = await self.create_irodori_tts_engine()
                except TypeError:
                    # Keep compatibility with lightweight manager/test adapters
                    # whose creator predates checkpoint-aware keyword args.
                    try:
                        irodori_engine = await self.create_irodori_tts_engine()
                    except Exception as exc:
                        print(
                            "[TTSManager] Irodori-TTS lazy initialization failed; "
                            f"keeping current engine ({type(exc).__name__})"
                        )
                        return
                except Exception as exc:
                    print(
                        "[TTSManager] Irodori-TTS lazy initialization failed; "
                        f"keeping current engine ({type(exc).__name__})"
                    )
                    return
                if irodori_engine is None:
                    return
                self.register_engine("irodori_tts", irodori_engine)

            if self.current_engine != "irodori_tts":
                self.set_engine("irodori_tts")
            return

        # The database is the source of truth for a character's preferred
        # engine.  Once a character has hot-switched to Irodori, leaving the
        # manager on Irodori after a later GUI change to an already-registered
        # engine would silently synthesize with the wrong voice.  Switch only
        # to engines that are already part of the normal lifecycle; unlike
        # Irodori, other engines retain their existing explicit startup and
        # initialization requirements.
        if preferred_engine in self.engines:
            if self.current_engine != preferred_engine:
                self.set_engine(preferred_engine)
            return

        # Do not produce an Irodori utterance for an explicitly persisted but
        # unavailable non-Irodori engine.  Clearing the current engine makes
        # synthesize fail closed (with the standard "No TTS engine available"
        # path) rather than speaking in an unintended voice.  If another
        # engine was already active, preserve its existing lifecycle behavior
        # and avoid a destructive cross-engine change.
        if self.current_engine == "irodori_tts":
            print(
                "[TTSManager] Preferred character engine is unavailable; "
                f"not continuing with Irodori ({preferred_engine})"
            )
            self.current_engine = None

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
        if not await asyncio.to_thread(engine.start_engine):
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
        if not await asyncio.to_thread(engine.start_engine):
            return None
            
        # Initialize client
        if not await engine.initialize():
            engine.stop_engine()
            return None
        
        # Get available speakers for debugging (non-blocking)
        try:
            print(f"[TTSManager] スピーカー情報を取得中...")
            # Create a task with timeout to avoid blocking
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

    async def create_miotts_engine(
        self,
        model_id: Optional[str] = None,
        codec_model_id: Optional[str] = None,
        default_preset_id: Optional[str] = None,
        device: Optional[str] = None,
    ) -> Optional[MioTTSEngine]:
        """Create and initialize embedded MioTTS engine."""
        miotts_settings = self.config.get('tts_settings', {}).get('miotts', {})

        engine = MioTTSEngine(
            model_id=model_id or miotts_settings.get('model_id'),
            codec_model_id=codec_model_id or miotts_settings.get('codec_model_id'),
            refs_dir=miotts_settings.get('refs_dir'),
            presets_dir=miotts_settings.get('presets_dir'),
            device=device or miotts_settings.get('device', 'auto'),
            dtype=miotts_settings.get('dtype', 'auto'),
            default_preset_id=(
                default_preset_id
                or miotts_settings.get('default_preset_id')
                or miotts_settings.get('preset_id')
            ),
            trust_remote_code=miotts_settings.get('trust_remote_code', False),
            max_text_length=miotts_settings.get('max_text_length', 300),
            max_reference_mb=miotts_settings.get('max_reference_mb', 20),
            max_reference_seconds=miotts_settings.get('max_reference_seconds', 20.0),
            temperature=miotts_settings.get('temperature', 0.8),
            top_p=miotts_settings.get('top_p', 1.0),
            max_tokens=miotts_settings.get('max_tokens', 700),
            repetition_penalty=miotts_settings.get('repetition_penalty', 1.0),
            presence_penalty=miotts_settings.get('presence_penalty', 0.0),
            frequency_penalty=miotts_settings.get('frequency_penalty', 0.0),
            best_of_n_enabled=miotts_settings.get('best_of_n_enabled', False),
            best_of_n_n=miotts_settings.get('best_of_n_n', 1),
        )

        if not await engine.initialize():
            return None

        return engine

    async def create_irodori_tts_engine(
        self,
        hf_checkpoint: Optional[str] = None,
        irodori_model: Optional[str] = None,
        codec_repo: Optional[str] = None,
        refs_dir: Optional[str] = None,
        use_gpu: Optional[bool] = None,
    ) -> Optional[Any]:
        """Create and initialize Irodori-TTS engine.

        Args:
            hf_checkpoint: Optional local/Hugging Face checkpoint override.  The
                explicit value is preserved, including v3 checkpoints.
            codec_repo: HuggingFace codec repo id
            refs_dir: Directory with reference voice audio files
            use_gpu: Whether to use GPU acceleration

        Returns:
            Initialized IrodoriTTSEngine or None
        """
        global IrodoriTTSEngine
        if IrodoriTTSEngine is None:
            from .engines.irodori_tts_engine import IrodoriTTSEngine as _IrodoriTTSEngine
            IrodoriTTSEngine = _IrodoriTTSEngine

        irodori_settings = self.config.get('tts_settings', {}).get('irodori_tts', {})
        normalize_irodori_settings(irodori_settings)

        # Keep an explicit checkpoint intact.  The v4.1 model is only the
        # default; direct callers may continue selecting v3 checkpoints.
        hf_checkpoint = resolve_irodori_checkpoint(
            {
                "hf_checkpoint": hf_checkpoint,
                "irodori_model": irodori_model,
            },
            fallback_settings=irodori_settings,
        )
        codec_repo = codec_repo or irodori_settings.get('codec_repo')
        refs_dir = refs_dir or irodori_settings.get('refs_dir')
        use_gpu = use_gpu if use_gpu is not None else irodori_settings.get('use_gpu', True)

        engine_kwargs = dict(
            hf_checkpoint=hf_checkpoint,
            codec_repo=codec_repo,
            refs_dir=refs_dir,
            model_device=irodori_settings.get('model_device', 'cuda'),
            codec_device=irodori_settings.get('codec_device', 'cuda'),
            model_precision=irodori_settings.get('model_precision', 'fp32'),
            codec_precision=irodori_settings.get('codec_precision', 'fp32'),
            use_gpu=use_gpu,
            num_steps=irodori_settings.get('num_steps', 40),
            t_schedule_mode=irodori_settings.get('t_schedule_mode', 'linear'),
            sway_coeff=irodori_settings.get('sway_coeff', -1.0),
            seconds=irodori_settings.get('seconds'),
            duration_scale=irodori_settings.get('duration_scale', 1.0),
            # None lets the vendored runtime use checkpoint metadata (120s for
            # v4.1, legacy 30s fallback for v2/v3).
            max_ref_seconds=irodori_settings.get('max_ref_seconds'),
            ref_normalize_db=irodori_settings.get('ref_normalize_db', -16.0),
            ref_ensure_max=irodori_settings.get('ref_ensure_max', True),
            cfg_scale_text=irodori_settings.get('cfg_scale_text', 3.0),
            cfg_scale_caption=irodori_settings.get('cfg_scale_caption', 3.0),
            cfg_scale_speaker=irodori_settings.get('cfg_scale_speaker', 5.0),
            config=self.config,
        )
        if irodori_model is not None:
            engine_kwargs["irodori_model"] = irodori_model
        engine = IrodoriTTSEngine(**engine_kwargs)
        
        # Initialize engine
        if not await engine.initialize():
            return None
        
        return engine

    @staticmethod
    def _irodori_settings_from_character(
        char_config: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Extract character-level Irodori settings from both JSON shapes."""

        if not isinstance(char_config, Mapping):
            return {}
        voice = char_config.get("voice")
        voice = voice if isinstance(voice, Mapping) else {}
        params = voice.get("parameters")
        params = params if isinstance(params, Mapping) else {}
        settings: dict[str, Any] = dict(params)
        # A few integrations put the selector alongside ``parameters``.  The
        # explicit voice-level value wins over a stale nested copy.
        for key in ("irodori_model", "hf_checkpoint", "voice_design_checkpoint"):
            if key in voice and voice.get(key) is not None:
                settings[key] = voice.get(key)
        return settings

    def _irodori_global_settings(self) -> Mapping[str, Any]:
        settings = self.config.get("tts_settings", {}).get("irodori_tts", {})
        return settings if isinstance(settings, Mapping) else {}

    @staticmethod
    def _engine_checkpoint(engine: Any) -> Optional[str]:
        value = getattr(engine, "hf_checkpoint", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    async def _irodori_engine_for_character(
        self,
        char_config: Mapping[str, Any] | None,
        current_engine: Any,
        override_settings: Mapping[str, Any] | None = None,
    ) -> Any:
        """Return an Irodori wrapper matching the character's checkpoint."""

        settings = self._irodori_settings_from_character(char_config)
        if isinstance(override_settings, Mapping):
            for key in ("irodori_model", "hf_checkpoint", "voice_design_checkpoint"):
                value = override_settings.get(key)
                if value is not None:
                    settings[key] = value
        global_settings = self._irodori_global_settings()
        checkpoint = resolve_irodori_checkpoint(
            settings,
            fallback_settings=global_settings,
        )

        # Existing custom/fake engines may not expose ``hf_checkpoint``.  With
        # no character selector, preserve the live object rather than forcing
        # a second engine (important for integrations and unit-test doubles).
        explicit_character_choice = any(
            key in settings
            for key in ("irodori_model", "hf_checkpoint", "voice_design_checkpoint")
        )
        current_checkpoint = self._engine_checkpoint(current_engine)
        if not explicit_character_choice and current_engine is not None:
            if current_checkpoint is None or current_checkpoint == checkpoint:
                return current_engine

        if current_engine is not None and current_checkpoint == checkpoint:
            self._irodori_engine_cache[checkpoint] = current_engine
            return current_engine
        cached = self._irodori_engine_cache.get(checkpoint)
        if cached is not None:
            return cached

        try:
            engine = await self.create_irodori_tts_engine(hf_checkpoint=checkpoint)
        except TypeError:
            # Compatibility with integrations exposing the pre-selector
            # ``create_irodori_tts_engine()`` signature.  If that adapter does
            # advertise a different checkpoint, reject it rather than
            # silently previewing/speaking with the wrong model.
            engine = await self.create_irodori_tts_engine()
            actual = self._engine_checkpoint(engine)
            if actual is not None and actual != checkpoint:
                return None
        if engine is None:
            return None
        self._irodori_engine_cache[checkpoint] = engine
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
        # GUI/API character edits are persisted in the ECC database while the
        # voice-chat manager keeps an in-memory snapshot.  Refresh just before
        # each utterance so caption/reference-asset changes apply immediately.
        await self._refresh_character_config_from_db(character_name)

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

        # Keep the character snapshot available for both engine selection and
        # parameter propagation.  Irodori is the one engine whose model can be
        # selected per character; all other engines retain the existing map.
        char_config = (
            self.character_configs.get(character_name)
            if character_name and character_name in self.character_configs
            else {}
        )
        engine = self.engines[self.current_engine]
        if self.current_engine == "irodori_tts":
            selected_engine = await self._irodori_engine_for_character(
                char_config,
                engine,
                kwargs,
            )
            if selected_engine is None:
                print("[TTSManager] Irodori checkpoint engine is unavailable")
                return None
            engine = selected_engine

        # 全TTS共通の誤読リスク検出。無効時はモデルをロードせず、失敗時も原文を維持する。
        try:
            preflight = await self.yomi_preflight.process(
                processed_text,
                engine_name=self.current_engine,
                engine=engine,
                config=self.config,
            )
            processed_text = preflight.final_text
        except Exception as exc:
            # 任意のプリフライトが想定外の例外を漏らしてもTTS本体は止めない。
            print(f"[TTSManager] Yomi Linter preflight warning: {exc}")
        
        # Get global speed adjustment from the DB-backed app config.
        speed_adjustment = self.config.get('tts', {}).get('speed_adjustment', 1.0)
        
        # Get character configuration if specified
        if character_name and character_name in self.character_configs:
            
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

            elif self.current_engine == "miotts" and isinstance(engine, MioTTSEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})

                miotts_keys = (
                    'temperature',
                    'top_p',
                    'max_tokens',
                    'max_new_tokens',
                    'repetition_penalty',
                    'presence_penalty',
                    'frequency_penalty',
                    'best_of_n_enabled',
                    'best_of_n_n',
                    'reference_data',
                    'reference_base64',
                    'reference_audio_base64',
                    'reference_audio_path',
                    'ref_wav',
                )
                miotts_kwargs = {
                    'voice_id': voice_config.get('voice_id', kwargs.get('voice_id')),
                    'voice_name': voice_config.get('voice_name', kwargs.get('voice_name')),
                    'preset_id': params.get('preset_id', kwargs.get('preset_id')),
                    'character_name': character_name,
                    'ref_wav': voice_config.get('ref_wav', kwargs.get('ref_wav')),
                    'reference_audio_path': voice_config.get(
                        'reference_audio_path',
                        kwargs.get('reference_audio_path'),
                    ),
                }
                for key in miotts_keys:
                    value = params.get(key, kwargs.get(key))
                    if value is not None:
                        miotts_kwargs[key] = value
                kwargs.update({key: value for key, value in miotts_kwargs.items() if value is not None})

            elif IrodoriTTSEngine is not None and self.current_engine == "irodori_tts" and isinstance(engine, IrodoriTTSEngine):
                voice_config = char_config.get('voice', {})
                params = voice_config.get('parameters', {})
                if not isinstance(params, Mapping):
                    params = {}

                # Handle voice selection
                voice_name = voice_config.get('voice_name', kwargs.get('voice_name'))

                # ``irodori_reference_assets`` is the character voice contract
                # used by the GUI/API.  Keep the list ordered and accept both
                # persisted ``relative_path`` and legacy ``path`` objects.
                raw_assets = voice_config.get(
                    'irodori_reference_assets',
                    params.get('irodori_reference_assets'),
                )
                asset_paths: list[str] = []
                if isinstance(raw_assets, list):
                    for asset in raw_assets:
                        if not isinstance(asset, Mapping):
                            continue
                        path = asset.get('relative_path') or asset.get('path')
                        if isinstance(path, str) and path.strip():
                            asset_paths.append(path.strip())

                # Explicit request/voice paths always win over persisted asset
                # metadata.  The old singular ref_wav/ref_latent fields remain
                # fully supported for v3 and existing character files.
                explicit_ref_wavs = kwargs.get('ref_wavs')
                if explicit_ref_wavs is None:
                    explicit_ref_wavs = voice_config.get(
                        'ref_wavs', params.get('ref_wavs')
                    )
                if not isinstance(explicit_ref_wavs, (list, tuple)):
                    explicit_ref_wavs = None
                if explicit_ref_wavs is not None:
                    selected_ref_wavs = list(explicit_ref_wavs)
                elif asset_paths:
                    selected_ref_wavs = asset_paths
                else:
                    selected_ref_wavs = None

                explicit_ref_latents = kwargs.get('ref_latents')
                if explicit_ref_latents is None:
                    explicit_ref_latents = voice_config.get(
                        'ref_latents', params.get('ref_latents')
                    )
                if not isinstance(explicit_ref_latents, (list, tuple)):
                    explicit_ref_latents = None

                irodori_kwargs = {
                    'voice_name': voice_name,
                    'character_name': character_name,
                    # Preserve the selector in the request contract for
                    # adapters/instrumentation; the wrapper itself has already
                    # resolved it to the checkpoint used by RuntimeKey.
                    'irodori_model': params.get(
                        'irodori_model', kwargs.get('irodori_model')
                    ),
                    'ref_wav': voice_config.get(
                        'ref_wav', params.get('ref_wav', kwargs.get('ref_wav'))
                    ),
                    'ref_wavs': selected_ref_wavs,
                    'ref_latent': voice_config.get(
                        'ref_latent', params.get('ref_latent', kwargs.get('ref_latent'))
                    ),
                    'ref_latents': explicit_ref_latents,
                    'caption': voice_config.get('caption', params.get('caption', kwargs.get('caption'))),
                    'no_ref': voice_config.get('no_ref', params.get('no_ref', kwargs.get('no_ref'))),
                    'seconds': params.get('seconds', kwargs.get('seconds')),
                    'min_seconds': params.get('min_seconds', kwargs.get('min_seconds')),
                    'max_seconds': params.get('max_seconds', kwargs.get('max_seconds')),
                    'max_ref_seconds': params.get(
                        'max_ref_seconds', kwargs.get('max_ref_seconds')
                    ),
                    'duration_scale': params.get(
                        'duration_scale', kwargs.get('duration_scale')
                    ),
                    'num_steps': params.get('num_steps', kwargs.get('num_steps')),
                    't_schedule_mode': params.get(
                        't_schedule_mode',
                        kwargs.get('t_schedule_mode'),
                    ),
                    'sway_coeff': params.get('sway_coeff', kwargs.get('sway_coeff')),
                    'ref_normalize_db': params.get('ref_normalize_db', kwargs.get('ref_normalize_db')),
                    'ref_ensure_max': params.get('ref_ensure_max', kwargs.get('ref_ensure_max')),
                    'cfg_scale_text': params.get('cfg_scale_text', kwargs.get('cfg_scale_text')),
                    'cfg_scale_caption': params.get('cfg_scale_caption', kwargs.get('cfg_scale_caption')),
                    'cfg_scale_speaker': params.get('cfg_scale_speaker', kwargs.get('cfg_scale_speaker')),
                    'seed': params.get('seed', kwargs.get('seed')),
                }
                kwargs.update({key: value for key, value in irodori_kwargs.items() if value is not None})
                
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
        # Character-specific Irodori wrappers live outside ``self.engines`` so
        # a manager can switch checkpoints without replacing the public engine
        # slot.  Clean both collections, but deduplicate by object identity
        # because the active wrapper is commonly present in both.
        engines = list(self.engines.values()) + list(self._irodori_engine_cache.values())
        seen: set[int] = set()
        try:
            for engine in engines:
                identity = id(engine)
                if identity in seen:
                    continue
                seen.add(identity)
                if hasattr(engine, 'stop_engine'):
                    engine.stop_engine()
                elif hasattr(engine, 'cleanup'):
                    if asyncio.iscoroutinefunction(engine.cleanup):
                        await engine.cleanup()
                    else:
                        engine.cleanup()
        finally:
            # Do not retain wrappers (and their runtime references) after the
            # manager lifecycle ends, even when one engine cleanup raises.
            self._irodori_engine_cache.clear()
