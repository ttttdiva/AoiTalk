"""
Speech recognition manager for handling multiple engines
"""
import numpy as np
from typing import Dict, Any, Optional, Generator, Tuple, Type
from .base import SpeechRecognizerInterface
from .hallucination_filter import HallucinationFilter


class SpeechRecognitionManager:
    """Manager for speech recognition engines with lazy loading"""
    
    # Registry of available engines - stores import paths instead of classes
    _engine_registry = {
        'whisper': '.engines.whisper_recognizer.WhisperSpeechRecognizer',
        'parakeet': '.engines.parakeet_recognizer.ParakeetRecognizer',
        'google': '.engines.google_speech_recognizer.GoogleSpeechRecognizer',
        'gemini': '.engines.gemini_recognizer.GeminiSpeechRecognizer',
    }
    
    # Cache for loaded engine classes
    _loaded_engines: Dict[str, Type[SpeechRecognizerInterface]] = {}
    
    def __init__(self, engine_name: str = 'whisper', config: Dict[str, Any] = None):
        """Initialize speech recognition manager
        
        Args:
            engine_name: Name of the speech recognition engine to use
            config: Configuration dictionary
        """
        self.engine_name = engine_name
        self.config = config or {}
        self.recognizer = self._create_recognizer()
        
        # Initialize hallucination filter
        self.hallucination_filter = HallucinationFilter(config)
        
        # Common hallucination detection settings (adjusted for WSL environment)
        self.hallucination_detection = self.config.get('hallucination_detection', True)
        self.min_audio_duration = self.config.get('min_audio_duration', 0.2)   # Shorter for responsiveness
        self.energy_threshold = self.config.get('energy_threshold', 0.000005)  # Even lower for WSL2 issues
        self.silence_ratio_threshold = self.config.get('silence_ratio_threshold', 0.90)  # More permissive
        
        # Validation cache for performance
        self._validation_cache = {}
        self._cache_max_size = 100
        
    def _load_engine_class(self, engine_name: str) -> Type[SpeechRecognizerInterface]:
        """Lazy load engine class
        
        Args:
            engine_name: Name of the engine to load
            
        Returns:
            Engine class
            
        Raises:
            ImportError: If engine cannot be imported
            ValueError: If engine name is unknown
        """
        if engine_name not in self._engine_registry:
            available = ', '.join(self._engine_registry.keys())
            raise ValueError(f"Unknown engine: {engine_name}. Available: {available}")
            
        # Check if already loaded
        if engine_name in self._loaded_engines:
            return self._loaded_engines[engine_name]
            
        # Import the engine module dynamically
        import_path = self._engine_registry[engine_name]
        module_path, class_name = import_path.rsplit('.', 1)
        
        try:
            print(f"[SpeechRecognitionManager] Lazy loading {engine_name} engine...")
            if module_path.startswith('.'):
                # Relative import
                from importlib import import_module
                module = import_module(module_path, package=__name__.rsplit('.', 1)[0])
            else:
                # Absolute import
                import importlib
                module = importlib.import_module(module_path)
                
            engine_class = getattr(module, class_name)
            
            # Verify it implements the interface
            if not issubclass(engine_class, SpeechRecognizerInterface):
                raise ValueError(f"Engine class {class_name} must implement SpeechRecognizerInterface")
                
            # Cache the loaded class
            self._loaded_engines[engine_name] = engine_class
            print(f"[SpeechRecognitionManager] Successfully loaded {engine_name} engine")
            
            return engine_class
            
        except ImportError as e:
            print(f"[SpeechRecognitionManager] Failed to import {engine_name} engine: {e}")
            raise ImportError(f"Cannot import {engine_name} engine: {e}")
    
    def _create_recognizer(self) -> SpeechRecognizerInterface:
        """Create recognizer instance
        
        Returns:
            Speech recognizer instance
            
        Raises:
            ValueError: If engine name is unknown
            ImportError: If engine cannot be imported
        """
        # Lazy load the engine class
        engine_class = self._load_engine_class(self.engine_name)
        
        engine_config = self.config.get('engines', {}).get(self.engine_name, {})
        
        # Add API key for Gemini if not provided in config
        if self.engine_name == 'gemini' and 'api_key' not in engine_config:
            from ..config import Config
            config = Config()
            gemini_api_key = config.get('gemini_api_key')
            if gemini_api_key:
                engine_config['api_key'] = gemini_api_key
        
        print(f"[SpeechRecognitionManager] Creating {self.engine_name} recognizer instance")
        return engine_class(engine_config)
    
    @classmethod
    def register_engine(cls, name: str, import_path: str) -> None:
        """Register a new speech recognition engine
        
        Args:
            name: Engine name
            import_path: Import path for the engine class (e.g., '.engines.custom.CustomRecognizer')
        """
        cls._engine_registry[name] = import_path
        print(f"[SpeechRecognitionManager] Registered engine: {name} -> {import_path}")
    
    @classmethod
    def list_engines(cls) -> list:
        """List available speech recognition engines
        
        Returns:
            List of engine names
        """
        return list(cls._engine_registry.keys())
    
    def _is_valid_audio(self, audio_data: bytes, sample_rate: int = 16000, 
                       channels: int = 1, sample_width: int = 2) -> bool:
        """Check if audio contains valid speech content (common validation)
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels  
            sample_width: Sample width in bytes
            
        Returns:
            True if audio seems to contain speech
        """
        # Skip validation if disabled
        if not self.hallucination_detection:
            return True
            
        # Check cache first
        cache_key = hash((len(audio_data), sample_rate, channels, sample_width))
        if cache_key in self._validation_cache:
            return self._validation_cache[cache_key]
            
        try:
            # Convert to numpy array
            if sample_width == 1:
                dtype = np.int8
                max_val = 127
            elif sample_width == 2:
                dtype = np.int16
                max_val = 32767
            elif sample_width == 4:
                dtype = np.int32
                max_val = 2147483647
            else:
                return True  # Unknown format, let engine handle it
                
            audio_array = np.frombuffer(audio_data, dtype=dtype)
            
            # Convert to mono if needed
            if channels > 1:
                audio_array = audio_array.reshape(-1, channels).mean(axis=1)
                
            # Convert to float [-1, 1]
            audio_float = audio_array.astype(np.float32) / max_val
            
            # Check duration
            duration = len(audio_float) / sample_rate
            print(f"[SpeechManager] Audio duration: {duration:.2f}s (min: {self.min_audio_duration}s)")
            if duration < self.min_audio_duration:
                print(f"[SpeechManager] Audio too short: {duration:.2f}s < {self.min_audio_duration}s")
                return False
            
            # Check energy level
            rms_energy = np.sqrt(np.mean(audio_float ** 2))
            print(f"[SpeechManager] Audio RMS energy: {rms_energy:.6f} (min: {self.energy_threshold})")
            if rms_energy < self.energy_threshold:
                print(f"[SpeechManager] Audio energy too low: {rms_energy:.6f} < {self.energy_threshold}")
                return False
            
            # Check silence ratio with more lenient calculation
            silence_threshold = max(rms_energy * 0.1, self.energy_threshold * 0.3)  # Even more lenient
            silence_samples = np.sum(np.abs(audio_float) < silence_threshold)
            silence_ratio = silence_samples / len(audio_float)
            
            print(f"[SpeechManager] Silence ratio: {silence_ratio:.2f} (max: {self.silence_ratio_threshold})")
            if silence_ratio > self.silence_ratio_threshold:
                print(f"[SpeechManager] Too much silence: {silence_ratio:.2f} > {self.silence_ratio_threshold}")
                return False
                
            print(f"[SpeechManager] ✅ Audio validation passed: duration={duration:.2f}s, energy={rms_energy:.6f}, silence={silence_ratio:.2f}")
            
            # Cache result
            result = True
            self._update_cache(cache_key, result)
            return result
            
        except Exception as e:
            print(f"[SpeechManager] Audio validation error: {e}")
            return True  # Let engine handle it
            
    def _update_cache(self, key, value):
        """Update validation cache with size limit"""
        self._validation_cache[key] = value
        
        # Limit cache size
        if len(self._validation_cache) > self._cache_max_size:
            # Remove oldest entries
            keys_to_remove = list(self._validation_cache.keys())[:-self._cache_max_size]
            for k in keys_to_remove:
                del self._validation_cache[k]
    
    def _clean_hallucination_prefix(self, text: str) -> str:
        """Remove hallucination prefixes while keeping the actual content (common cleaning)
        
        Args:
            text: Recognized text that may contain hallucination prefixes
            
        Returns:
            Cleaned text with hallucination prefixes removed
        """
        if not text:
            return text
            
        original_text = text
        
        # List of hallucination prefixes to remove (common across engines)
        hallucination_prefixes = [
            '心の声', '心の声。', '心の声：', '心の声、', '心の声 ',
            '心の声：「', '心の声:「', '心の声「',
            'ナレーション', 'ナレーション：', 'ナレーション。',
            'モノローグ', 'モノローグ：', 'モノローグ。',
            'テロップ', 'テロップ：', 'テロップ。',
            '字幕', '字幕：', '字幕。',
        ]
        
        # Remove prefixes from the beginning
        for prefix in hallucination_prefixes:
            if text.startswith(prefix):
                # Remove the prefix
                text = text[len(prefix):].strip()
                
                # Also remove common following punctuation
                if text.startswith('：') or text.startswith(':'):
                    text = text[1:].strip()
                if text.startswith('「'):
                    text = text[1:].strip()
                if text.endswith('」'):
                    text = text[:-1].strip()
                    
                print(f"[SpeechManager] Removed hallucination prefix '{prefix}' from '{original_text}' → '{text}'")
                break
                
        return text
    
    
    def _is_high_quality_audio(self, audio_data: bytes, sample_rate: int = 16000, 
                              channels: int = 1, sample_width: int = 2) -> bool:
        """Check if audio meets high quality standards for Gemini
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels  
            sample_width: Sample width in bytes
            
        Returns:
            True if audio meets high quality standards
        """
        try:
            # Convert to numpy array
            if sample_width == 1:
                dtype = np.int8
                max_val = 127
            elif sample_width == 2:
                dtype = np.int16
                max_val = 32767
            elif sample_width == 4:
                dtype = np.int32
                max_val = 2147483647
            else:
                return True  # Unknown format, let engine handle it
                
            audio_array = np.frombuffer(audio_data, dtype=dtype)
            
            # Convert to mono if needed
            if channels > 1:
                audio_array = audio_array.reshape(-1, channels).mean(axis=1)
                
            # Convert to float [-1, 1]
            audio_float = audio_array.astype(np.float32) / max_val
            
            # Realistic duration requirement for Gemini
            duration = len(audio_float) / sample_rate
            if duration < 0.5:  # Require at least 0.5 seconds (more reasonable)
                print(f"[SpeechManager] Audio too short for Gemini: {duration:.2f}s < 0.5s")
                return False
            
            # Reasonable energy requirement for Gemini
            rms_energy = np.sqrt(np.mean(audio_float ** 2))
            if rms_energy < 0.002:  # Much more reasonable threshold
                print(f"[SpeechManager] Audio energy too low for Gemini: {rms_energy:.6f} < 0.002")
                return False
            
            # Check for speech-like characteristics
            # Calculate zero crossing rate (should be moderate for speech)
            zero_crossings = np.sum(np.diff(np.sign(audio_float)) != 0)
            zcr = zero_crossings / len(audio_float)
            
            if zcr < 0.005 or zcr > 0.5:  # More lenient thresholds
                print(f"[SpeechManager] Abnormal zero crossing rate for Gemini: {zcr:.3f}")
                return False
            
            # Check for clipping (saturated audio)
            clipping_ratio = np.sum(np.abs(audio_float) > 0.95) / len(audio_float)
            if clipping_ratio > 0.01:  # More than 1% clipped
                print(f"[SpeechManager] Audio clipping detected for Gemini: {clipping_ratio:.3f}")
                return False
                
            print(f"[SpeechManager] ✅ High quality audio for Gemini: duration={duration:.2f}s, energy={rms_energy:.6f}, zcr={zcr:.3f}")
            return True
            
        except Exception as e:
            print(f"[SpeechManager] High quality audio check error: {e}")
            return True  # Let engine handle it
    
    def _post_process_recognition(self, text: str) -> Optional[str]:
        """Post-process recognition result with common hallucination handling
        
        Args:
            text: Raw recognition result
            
        Returns:
            Cleaned text or None if it's a hallucination
        """
        if not self.hallucination_detection:
            return text
            
        if not text:
            return None
            
        # Clean hallucination prefixes first
        cleaned_text = self._clean_hallucination_prefix(text)
        
        # Use the unified hallucination filter
        if self.hallucination_filter.is_hallucination(cleaned_text, engine=self.engine_name):
            print(f"[SpeechManager] Hallucination detected: '{text}' → filtered out")
            return None
            
        # Return cleaned text if it's different from original
        if cleaned_text != text:
            print(f"[SpeechManager] Text cleaned: '{text}' → '{cleaned_text}'")
            
        return cleaned_text
    
    def switch_engine(self, engine_name: str, config: Dict[str, Any] = None) -> None:
        """Switch to different recognition engine
        
        Args:
            engine_name: Name of the engine to switch to
            config: Optional configuration for the new engine
        """
        print(f"[SpeechRecognitionManager] Switching from {self.engine_name} to {engine_name}")
        
        self.engine_name = engine_name
        if config:
            if 'engines' not in self.config:
                self.config['engines'] = {}
            self.config['engines'][engine_name] = config
            
        self.recognizer = self._create_recognizer()
        print(f"[SpeechRecognitionManager] Successfully switched to {engine_name}")
    
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the current recognition engine
        
        Args:
            config: Configuration dictionary
        """
        self.recognizer.configure(config)
    
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the current recognition engine
        
        Returns:
            Dictionary with engine information
        """
        return self.recognizer.get_engine_info()
    
    def add_assistant_output(self, text: str) -> None:
        """Add assistant output to hallucination filter context
        
        Args:
            text: Assistant's output text for echo detection
        """
        self.hallucination_filter.add_output_context(text)
    
    # Delegate all recognition methods to the current engine
    
    def recognize(self, 
                  audio_data: bytes, 
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,
                  prompt: Optional[str] = None) -> Optional[str]:
        """Recognize speech from audio data
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            language: Language code for recognition
            prompt: Optional prompt to guide recognition
            
        Returns:
            Recognized text or None if failed
        """
        # Common audio validation with stricter checks for Gemini
        if self.hallucination_detection and not self._is_valid_audio(audio_data, sample_rate, channels, sample_width):
            print("[SpeechManager] Audio validation failed, skipping recognition")
            return None
        
        # Additional validation for Gemini engine (stricter quality requirements)
        if self.engine_name == 'gemini':
            if not self._is_high_quality_audio(audio_data, sample_rate, channels, sample_width):
                print("[SpeechManager] Audio quality too low for Gemini, skipping recognition")
                return None
            
        # Call engine-specific recognition
        raw_result = self.recognizer.recognize(
            audio_data, sample_rate, channels, sample_width, language, prompt
        )
        
        # Apply common post-processing
        return self._post_process_recognition(raw_result)
    
    def start_stream(self) -> None:
        """Start a new streaming session"""
        self.recognizer.start_stream()
    
    def process_audio_chunk(self, 
                           audio_data: bytes,
                           sample_rate: int = 16000,
                           channels: int = 1,
                           sample_width: int = 2) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """Process audio chunk and yield transcription results
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            
        Yields:
            Tuple of (is_final, text) where is_final indicates if the segment is complete
        """
        return self.recognizer.process_audio_chunk(
            audio_data, sample_rate, channels, sample_width
        )
    
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and get final transcription
        
        Returns:
            Final transcription text or None
        """
        return self.recognizer.finish_stream()


# Decorator for registering speech engines
def register_speech_engine(name: str, import_path: str = None):
    """Decorator for registering speech engines
    
    Args:
        name: Engine name
        import_path: Optional import path (if not provided, will be auto-generated)
        
    Returns:
        Decorator function
    """
    def decorator(engine_class):
        # Auto-generate import path if not provided
        if import_path is None:
            module_path = engine_class.__module__
            class_name = engine_class.__name__
            full_path = f"{module_path}.{class_name}"
        else:
            full_path = import_path
            
        SpeechRecognitionManager.register_engine(name, full_path)
        return engine_class
    return decorator