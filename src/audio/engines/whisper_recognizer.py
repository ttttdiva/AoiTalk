"""
OpenAI Whisper speech recognition implementation
"""
import numpy as np
import time
from typing import Optional, Generator, Tuple, Dict, Any
from collections import deque
from ..hallucination_filter import HallucinationFilter

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    print("[WhisperRecognizer] torch not available, using CPU mode")

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("[WhisperRecognizer] whisper not available")

from ..base import SpeechRecognizerInterface


class WhisperSpeechRecognizer(SpeechRecognizerInterface):
    """OpenAI Whisper speech recognition implementation"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize Whisper speech recognizer
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        self.model_name = self.config.get('model', 'base')
        self.language = self.config.get('language', 'ja')
        self.chunk_length = self.config.get('chunk_length', 1.0)  # Reduced for faster response
        self.sample_rate = 16000  # Whisper expects 16kHz
        
        # Initialize Whisper model
        self.model = None
        if WHISPER_AVAILABLE:
            try:
                print(f"[WhisperRecognizer] Loading whisper model '{self.model_name}'...")
                self.model = whisper.load_model(self.model_name)
                print(f"[WhisperRecognizer] Model '{self.model_name}' loaded successfully")
            except Exception as e:
                print(f"[WhisperRecognizer] Failed to load model '{self.model_name}': {type(e).__name__}: {e}")
                # Try to load a smaller model as fallback
                if self.model_name != "base":
                    try:
                        print("[WhisperRecognizer] Trying to load 'base' model as fallback...")
                        self.model = whisper.load_model("base")
                        self.model_name = "base"
                        print("[WhisperRecognizer] Fallback to 'base' model successful")
                    except Exception as e2:
                        print(f"[WhisperRecognizer] Failed to load fallback model: {type(e2).__name__}: {e2}")
        else:
            print("[WhisperRecognizer] Whisper is not installed. Please run: pip install openai-whisper")
            raise ImportError("openai-whisper is required but not installed")
            
        # Buffer for streaming mode
        self.audio_buffer = deque()
        self.buffer_duration = 0.0
        self.context_text = ""  # Previous transcription for context
        
        # Initialize hallucination filter
        self.hallucination_filter = HallucinationFilter(config)
        
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        old_model = self.model_name
        self.config.update(config)
        new_model = self.config.get('model', self.model_name)
        
        # Reload model if changed
        if new_model != old_model and WHISPER_AVAILABLE:
            try:
                print(f"[WhisperRecognizer] Switching model from '{old_model}' to '{new_model}'")
                self.model = whisper.load_model(new_model)
                self.model_name = new_model
                print(f"[WhisperRecognizer] Successfully switched to model '{new_model}'")
            except Exception as e:
                print(f"[WhisperRecognizer] Failed to switch model: {e}")
                # Keep the old model
        
        # Update other configuration
        self.language = self.config.get('language', self.language)
        self.chunk_length = self.config.get('chunk_length', self.chunk_length)
        
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        return {
            'engine': 'whisper',
            'model': self.model_name,
            'language': self.language,
            'chunk_length': self.chunk_length,
            'torch_available': TORCH_AVAILABLE,
            'cuda_available': TORCH_AVAILABLE and torch.cuda.is_available(),
            'model_loaded': self.model is not None
        }
        
    def start_stream(self):
        """Start a new streaming session"""
        self.audio_buffer.clear()
        self.buffer_duration = 0.0
        self.context_text = ""
        print("[WhisperRecognizer] Started new streaming session")
        
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
        if not self.model:
            return
            
        # Convert audio to numpy array
        audio_array = self._audio_to_numpy(audio_data, sample_rate, channels, sample_width)
        
        # Add to buffer
        self.audio_buffer.extend(audio_array)
        chunk_duration = len(audio_array) / self.sample_rate
        self.buffer_duration += chunk_duration
        
        # Process if we have enough audio
        if self.buffer_duration >= self.chunk_length:
            # Convert buffer to array
            buffer_array = np.array(self.audio_buffer)
            
            # Clear buffer
            self.audio_buffer.clear()
            self.buffer_duration = 0.0
            
            # Transcribe the chunk
            try:
                # Use context from previous transcription
                prompt = self.context_text[-224:] if self.context_text else None
                
                result = self.model.transcribe(
                    buffer_array,
                    language=self.language,
                    initial_prompt=prompt,
                    temperature=0.0,
                    no_speech_threshold=0.6,
                    logprob_threshold=-1.0,
                    compression_ratio_threshold=2.4,
                    fp16=TORCH_AVAILABLE and torch.cuda.is_available()
                )
                
                text = result["text"].strip()
                
                if text and not self.hallucination_filter.is_hallucination(text, engine='whisper'):
                    # Update context
                    self.context_text = (self.context_text + " " + text).strip()
                    
                    # Yield the transcription
                    yield (True, text)  # Mark as final since we process complete chunks
                    
            except Exception as e:
                print(f"[WhisperRecognizer] Transcription error: {type(e).__name__}: {e}")
                
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and process remaining audio
        
        Returns:
            Final transcription text or None
        """
        if not self.model or len(self.audio_buffer) == 0:
            return None
            
        # Process remaining audio in buffer
        try:
            buffer_array = np.array(self.audio_buffer)
            
            # Use context from previous transcription
            prompt = self.context_text[-224:] if self.context_text else None
            
            result = self.model.transcribe(
                buffer_array,
                language=self.language,
                initial_prompt=prompt,
                temperature=0.0,
                fp16=TORCH_AVAILABLE and torch.cuda.is_available()
            )
            
            text = result["text"].strip()
            
            # Clear buffer
            self.audio_buffer.clear()
            self.buffer_duration = 0.0
            
            if text and not self.hallucination_filter.is_hallucination(text, engine='whisper'):
                return text
            else:
                return None
                
        except Exception as e:
            print(f"[WhisperRecognizer] Final transcription error: {type(e).__name__}: {e}")
            return None
    
    def recognize(self, 
                  audio_data: bytes, 
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,
                  prompt: Optional[str] = None) -> Optional[str]:
        """Recognize speech from audio data (non-streaming)
        
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
        if not self.model:
            print("[WhisperRecognizer] Whisper model not loaded")
            return None
            
        try:
            # Convert audio to numpy
            audio_array = self._audio_to_numpy(audio_data, sample_rate, channels, sample_width)
            
            # Transcribe
            lang = language or self.language
            result_dict = self.model.transcribe(
                audio_array,
                language=lang,
                initial_prompt=prompt,
                temperature=0.0,
                fp16=TORCH_AVAILABLE and torch.cuda.is_available()
            )
            
            result = result_dict["text"].strip()
            print(f"[WhisperRecognizer] Transcription result: '{result}'")
            
            if result and not self.hallucination_filter.is_hallucination(result, engine='whisper'):
                return result
            else:
                return None
                
        except Exception as e:
            print(f"[WhisperRecognizer] Recognition error: {type(e).__name__}: {e}")
            return None
            
        
    def _audio_to_numpy(self,
                       audio_data: bytes,
                       sample_rate: int,
                       channels: int,
                       sample_width: int) -> np.ndarray:
        """Convert raw audio data to numpy array
        
        Args:
            audio_data: Raw audio data
            sample_rate: Sample rate
            channels: Number of channels
            sample_width: Sample width in bytes
            
        Returns:
            Numpy array of audio data in float32 format
        """
        # Convert bytes to numpy array
        if sample_width == 1:
            dtype = np.int8
        elif sample_width == 2:
            dtype = np.int16
        elif sample_width == 4:
            dtype = np.int32
        else:
            raise ValueError(f"Unsupported sample width: {sample_width}")
            
        audio_array = np.frombuffer(audio_data, dtype=dtype)
        
        # Convert to float32 and normalize
        audio_array = audio_array.astype(np.float32)
        if dtype == np.int16:
            audio_array /= 32768.0
        elif dtype == np.int32:
            audio_array /= 2147483648.0
        elif dtype == np.int8:
            audio_array /= 128.0
            
        # Handle multi-channel audio
        if channels > 1:
            # Reshape to (samples, channels) and take mean
            audio_array = audio_array.reshape(-1, channels).mean(axis=1)
            
        # Resample if needed (Whisper expects 16kHz)
        if sample_rate != 16000:
            try:
                import scipy.signal
                duration = len(audio_array) / sample_rate
                target_samples = int(duration * 16000)
                audio_array = scipy.signal.resample(audio_array, target_samples)
            except ImportError:
                print("[WhisperRecognizer] scipy not available, skipping resampling")
                
        return audio_array