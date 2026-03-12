"""
VOICEROID TTS engine implementation
注意: このエンジンはWindowsでのみ動作します
"""
import csv
import platform
from pathlib import Path
from typing import Optional, List
import warnings


def _normalize_voice_key(value: str) -> str:
    """Normalize strings for comparison (lowercase alphanumerics only)"""
    if value is None:
        return ""
    return ''.join(ch.lower() for ch in value if ch.isalnum())


def _load_voice_aliases() -> dict:
    """Load mapping of human-readable names to internal VOICEROID IDs"""
    aliases = {
        # Manual fallbacks forよく使う英語表記
        _normalize_voice_key('Kotonoha Aoi'): 'aoi_emo_44',
        _normalize_voice_key('Kotonoha Akane'): 'akane_west_emo_44',
        _normalize_voice_key('Tsukuyomi Ai'): 'ai_44',
        _normalize_voice_key('Tohoku Kiritan'): 'kiritan_44',
        _normalize_voice_key('Tohoku Itako'): 'itako_emo_44',
    }

    try:
        csv_path = Path(__file__).resolve().parents[3] / 'docs' / 'characters_voiceroid2.csv'
        if csv_path.exists():
            with csv_path.open(encoding='utf-8') as csv_file:
                reader = csv.DictReader(csv_file)
                for row in reader:
                    name = row.get('name')
                    voice = row.get('voice')
                    if name and voice:
                        aliases[_normalize_voice_key(name)] = voice.strip()
    except Exception:
        # 失敗時は手動マップのみ使用
        pass

    return aliases


VOICE_NAME_ALIASES = _load_voice_aliases()


def resolve_voiceroid_voice_id(name: Optional[str]) -> Optional[str]:
    """Resolve user-friendly voice name to internal VOICEROID id"""
    if not name:
        return None
    normalized = _normalize_voice_key(name)
    if not normalized:
        return None
    return VOICE_NAME_ALIASES.get(normalized)

# Windows環境でのみpyvcroid2をインポート
if platform.system() == "Windows":
    try:
        import pyvcroid2
        VOICEROID_AVAILABLE = True
    except ImportError:
        VOICEROID_AVAILABLE = False
        pyvcroid2 = None
else:
    VOICEROID_AVAILABLE = False
    pyvcroid2 = None


class VoiceroidEngine:
    """VOICEROID Text-to-Speech engine (Windows only)"""
    
    def __init__(self):
        """Initialize VOICEROID engine"""
        self.vc = None
        self.is_initialized = False
        self.available_voices = []
        self.current_voice = None
        self.current_voice_index = None
        
        if not VOICEROID_AVAILABLE:
            if platform.system() != "Windows":
                warnings.warn("VOICEROID is only available on Windows platforms")
            else:
                warnings.warn("pyvcroid2 library not found. Please install VOICEROID and pyvcroid2")
                
    async def initialize(self) -> bool:
        """Initialize VOICEROID engine
        
        Returns:
            True if initialization successful
        """
        if not VOICEROID_AVAILABLE:
            print("[VOICEROID] Engine not available (Windows/pyvcroid2 required)")
            return False
            
        try:
            # Initialize VOICEROID
            self.vc = pyvcroid2.VcRoid2()
            
            # Load language library
            lang_list = self.vc.listLanguages()
            if "standard" in lang_list:
                self.vc.loadLanguage("standard")
            elif len(lang_list) > 0:
                self.vc.loadLanguage(lang_list[0])
            else:
                print("[VOICEROID] No language library found")
                return False
                
            # Get available voices
            self.available_voices = self.vc.listVoices()
            if len(self.available_voices) == 0:
                print("[VOICEROID] No voice library found")
                return False

            print("[VOICEROID] Available voices:")
            for idx, voice in enumerate(self.available_voices):
                print(f"  [{idx}] {voice}")
                
            # Initialize without loading any voice (sample.py style)
            self.current_voice = None
            self.current_voice_index = None
            
            self.is_initialized = True
            return True
            
        except Exception as e:
            print(f"[VOICEROID] Initialization failed: {type(e).__name__}: {e}")
            if self.vc:
                try:
                    self.vc.close()
                except:
                    pass
                self.vc = None
            return False
            
    def set_voice(self, voice_name: str) -> bool:
        """Set voice by name (legacy method, use load_voice_directly instead)"""
        if not self.is_initialized or not self.vc:
            return False
            
        if voice_name in self.available_voices:
            voice_index = self.available_voices.index(voice_name)
            return self.load_voice_directly(voice_index)
        return False
            
    def set_voice_by_index(self, voice_index: int) -> bool:
        """Set voice by index (legacy method, use load_voice_directly instead)"""
        return self.load_voice_directly(voice_index)
            
    def load_voice_directly(self, voice_index: int) -> bool:
        """Load voice directly using sample.py method"""
        if not self.is_initialized or not self.vc:
            return False
            
        if 0 <= voice_index < len(self.available_voices):
            try:
                voice_label = self.available_voices[voice_index]
                print(f"[VOICEROID] load_voice_directly -> {voice_label} (index={voice_index})")
                self.vc.loadVoice(voice_label)
                self.current_voice = self.available_voices[voice_index]
                self.current_voice_index = voice_index
                return True
            except Exception:
                return False
        return False
            
    def _find_voice_index_by_string(self, identifier: Optional[str]) -> Optional[int]:
        """Return index by matching internal voice id/name"""
        if not identifier:
            return None
        normalized_identifier = _normalize_voice_key(identifier)
        if not normalized_identifier:
            return None
        for idx, voice in enumerate(self.available_voices):
            if normalized_identifier == _normalize_voice_key(voice):
                return idx
        return None

    def _find_voice_index_by_name(self, voice_name: Optional[str]) -> Optional[int]:
        """Return index by human-readable name using alias table"""
        if not voice_name:
            return None
        normalized_name = _normalize_voice_key(voice_name)
        if not normalized_name:
            return None

        # Direct match against available voices first
        direct_match = self._find_voice_index_by_string(voice_name)
        if direct_match is not None:
            return direct_match

        # Lookup alias (JP/EN → internal voice id)
        alias_voice = VOICE_NAME_ALIASES.get(normalized_name)
        if alias_voice:
            alias_match = self._find_voice_index_by_string(alias_voice)
            if alias_match is not None:
                print(
                    f"[VOICEROID] Voice name '{voice_name}' resolved to internal id '{alias_voice}'"
                )
                return alias_match

        return None

    def _normalize_voice_index(self, voice_index: Optional[int]) -> Optional[int]:
        """Validate voice index against the available voices list"""
        if voice_index is None:
            return None
        if not self.available_voices:
            print("[VOICEROID] No voices available to validate index")
            return None
        try:
            index = int(voice_index)
        except (TypeError, ValueError):
            print(f"[VOICEROID] Invalid voice index type: {voice_index}")
            return None

        if 0 <= index < len(self.available_voices):
            return index

        print(
            f"[VOICEROID] Voice index {index} out of range (0-{len(self.available_voices) - 1})."
        )
        return None

    def _sanitize_text(self, text: str) -> str:
        """Strip characters VOICEROID (Shift_JIS) cannot encode"""
        if not text:
            return text
        try:
            sanitized = text.encode('cp932', errors='ignore').decode('cp932', errors='ignore')
            if sanitized != text:
                print("[VOICEROID] Removed unsupported characters from text before synthesis")
            return sanitized or text
        except Exception:
            return text

    async def synthesize(self, 
                        text: str,
                        volume: float = 1.9,
                        speed: float = 1.35,
                        pitch: float = 1.1,
                        emphasis: float = 1.0,
                        pause_middle: int = 150,
                        pause_long: int = 370,
                        pause_sentence: int = 800,
                        master_volume: float = 1.0,
                        **kwargs) -> Optional[bytes]:
        """Synthesize speech from text"""
        if not self.is_initialized or not self.vc:
            print("[VOICEROID] Engine not initialized")
            return None
            
        try:
            sanitized_text = self._sanitize_text(text)
            # Voice selection from kwargs
            voice_index = kwargs.get('voice_index')
            voice_name = kwargs.get('voice_name')
            voice_id = kwargs.get('voice_id')

            # If voice_name provided, convert to index
            target_index = None
            if voice_id:
                target_index = self._find_voice_index_by_string(voice_id)
                if target_index is None:
                    print(
                        f"[VOICEROID] Voice id '{voice_id}' not found. "
                        f"Available voices: {self.available_voices}"
                    )

            if target_index is None and voice_name:
                target_index = self._find_voice_index_by_name(voice_name)
                if target_index is None:
                    print(
                        f"[VOICEROID] Voice name '{voice_name}' not found. "
                        f"Available voices: {self.available_voices}"
                    )

            # Ensure some voice is loaded before synthesis
            normalized_voice_index = (
                target_index
                if target_index is not None
                else self._normalize_voice_index(voice_index)
            )
            print(
                f"[VOICEROID] Requested voice -> id={voice_id}, name={voice_name}, "
                f"index={voice_index}, resolved_index={normalized_voice_index}"
            )

            if not self.available_voices:
                print("[VOICEROID] No voices available for synthesis")
                return None

            if self.current_voice is None:
                # Use requested voice if provided, otherwise default to the first entry
                fallback_index = (
                    normalized_voice_index
                    if normalized_voice_index is not None
                    else 0
                )
                fallback_index = self._normalize_voice_index(fallback_index)
                if fallback_index is None:
                    print("[VOICEROID] Failed to determine a valid initial voice")
                    return None
                if not self.load_voice_directly(fallback_index):
                    print(f"[VOICEROID] Failed to load initial voice (index={fallback_index})")
                    return None
            # Switch voice if requested and different from current
            elif (
                normalized_voice_index is not None
                and self.current_voice_index != normalized_voice_index
            ):
                if not self.load_voice_directly(normalized_voice_index):
                    print(
                        f"[VOICEROID] Failed to switch voice to index {normalized_voice_index}"
                    )
                    return None

            # Set parameters
            self.vc.param.volume = max(0.0, min(2.0, volume))
            self.vc.param.speed = max(0.5, min(4.0, speed))
            self.vc.param.pitch = max(0.5, min(2.0, pitch))
            self.vc.param.emphasis = max(0.0, min(2.0, emphasis))
            self.vc.param.pauseMiddle = max(80, min(500, pause_middle))
            self.vc.param.pauseLong = max(100, min(2000, pause_long))
            self.vc.param.pauseSentence = max(200, min(2000, pause_sentence))
            self.vc.param.masterVolume = max(0.0, min(2.0, master_volume))
            
            # Synthesize
            speech, _ = self.vc.textToSpeech(sanitized_text)
            if speech:
                return speech
            print("[VOICEROID] textToSpeech returned empty data")
            return None
            
        except Exception as e:
            print(f"[VOICEROID] Synthesis failed: {type(e).__name__}: {e}")
            return None
            
    def get_voices(self) -> List[str]:
        """Get list of available voices"""
        return self.available_voices.copy() if self.available_voices else []
        
    def get_current_voice(self) -> Optional[str]:
        """Get current voice name"""
        return self.current_voice
            
    def cleanup(self):
        """Cleanup VOICEROID resources"""
        if self.vc:
            try:
                self.vc.close()
            except:
                pass
            self.vc = None
        self.is_initialized = False
        
    def __del__(self):
        """Cleanup on deletion"""
        self.cleanup()
        
    def __enter__(self):
        """Context manager entry"""
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()
