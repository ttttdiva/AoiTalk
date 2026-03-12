"""
Parakeet ASR recognition implementation
NVIDIA Parakeet-based speech recognition for high-quality Japanese ASR
"""
import os
import tempfile
import numpy as np
import wave
from typing import Optional, Generator, Tuple, Dict, Any

try:
    # Fix lzma import issue
    import sys
    try:
        import lzma
    except ImportError:
        try:
            from backports import lzma
            sys.modules['lzma'] = lzma
            # Moved print to init method to avoid startup noise
        except ImportError:
            print("[ParakeetRecognizer] Warning: lzma module not available")
    
    import nemo.collections.asr as parakeet_asr
    from omegaconf import OmegaConf, open_dict
    PARAKEET_AVAILABLE = True
except (ImportError, ModuleNotFoundError) as e:
    PARAKEET_AVAILABLE = False
    print(f"[ParakeetRecognizer] nemo-toolkit not available: {e}")

from ..base import SpeechRecognizerInterface


class ParakeetRecognizer(SpeechRecognizerInterface):
    """NVIDIA Parakeet ASR implementation for Japanese speech recognition
    
    Features:
    - State-of-the-art Japanese ASR with punctuation
    - GPU acceleration support
    - Multiple decoder types (TDT/CTC)
    - Low hallucination rate
    - Trained on 35k+ hours of Japanese speech
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize Parakeet recognizer
        
        Args:
            config: Configuration dictionary with:
                - model: Model name (default: 'nvidia/parakeet-tdt_ctc-0.6b-ja')
                - device: Device to use ('cuda' or 'cpu')
                - batch_size: Batch size for processing
                - decoder_type: 'tdt' or 'ctc' (default: 'tdt')
                - compute_timestamps: Whether to compute word timestamps
                - cache_dir: Directory for model cache
        """
        self.config = config or {}
        
        # Show debug message only when actually creating the recognizer
        if 'lzma' in sys.modules and hasattr(sys.modules['lzma'], '__name__') and 'backports' in sys.modules['lzma'].__name__:
            print("[ParakeetRecognizer] Using backports.lzma")
        
        if not PARAKEET_AVAILABLE:
            raise ImportError(
                "nemo-toolkit is required but not installed. "
                "Install with: pip install nemo-toolkit[asr]"
            )
        
        # Default configuration
        self.model_name = self.config.get('model', 'nvidia/parakeet-tdt_ctc-0.6b-ja')
        self.device = self.config.get('device', self._get_default_device())
        self.batch_size = self.config.get('batch_size', 1)
        self.decoder_type = self.config.get('decoder_type', 'tdt')
        self.compute_timestamps = self.config.get('compute_timestamps', False)
        self.cache_dir = self.config.get('cache_dir', os.path.expanduser('~/.cache/nemo'))
        
        # Audio validation is now handled by SpeechRecognitionManager
        
        # Streaming buffer
        self.stream_buffer = []
        self.stream_sample_rate = 16000
        self.stream_chunk_duration = self.config.get('stream_chunk_duration', 1.0)  # Reduced for faster response
        
        # Reusable temp file for better performance
        self._temp_file_path = None
        self._create_temp_file()
        
        self.model = None
        self._initialize_model()
        
    def _get_default_device(self) -> str:
        """Get default device based on availability"""
        try:
            import torch
            return 'cuda' if torch.cuda.is_available() else 'cpu'
        except ImportError:
            return 'cpu'
            
    def _initialize_model(self):
        """Initialize the NeMo model"""
        try:
            print(f"[ParakeetRecognizer] Loading model: {self.model_name}")
            print(f"[ParakeetRecognizer] Device: {self.device}, Decoder: {self.decoder_type}")
            
            # Set cache directory
            os.environ['NEMO_CACHE_DIR'] = self.cache_dir
            
            # Load model
            self.model = parakeet_asr.models.ASRModel.from_pretrained(
                self.model_name,
                map_location=self.device
            )
            
            # Move model to device
            if self.device == 'cuda':
                self.model = self.model.cuda()
            else:
                self.model = self.model.cpu()
            
            # Configure decoding
            self._configure_decoding()
            
            # Set to eval mode
            self.model.eval()
            
            print(f"[ParakeetRecognizer] Model loaded successfully")
            
        except Exception as e:
            print(f"[ParakeetRecognizer] Failed to initialize model: {e}")
            self.model = None
            raise
            
    def _configure_decoding(self):
        """Configure model decoding settings"""
        if not self.model:
            return
            
        try:
            decoding_cfg = self.model.cfg.decoding
            with open_dict(decoding_cfg):
                # Set decoder type
                if hasattr(decoding_cfg, 'strategy'):
                    decoding_cfg.strategy = self.decoder_type
                    
                # Configure timestamps
                if self.compute_timestamps:
                    decoding_cfg.preserve_alignments = True
                    decoding_cfg.compute_timestamps = True
                    if hasattr(decoding_cfg, 'segment_seperators'):
                        decoding_cfg.segment_seperators = ["。", "？", "！"]
                    if hasattr(decoding_cfg, 'word_seperator'):
                        decoding_cfg.word_seperator = " "
                        
            self.model.change_decoding_strategy(decoding_cfg)
            
        except Exception as e:
            print(f"[ParakeetRecognizer] Warning: Could not configure decoding: {e}")
            
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        self.config.update(config)
        
        # Update settings that can be changed without reloading model
        if 'batch_size' in config:
            self.batch_size = config['batch_size']
            
        if 'stream_chunk_duration' in config:
            self.stream_chunk_duration = config['stream_chunk_duration']
        
        # Update decoder type if changed
        if 'decoder_type' in config and config['decoder_type'] != self.decoder_type:
            self.decoder_type = config['decoder_type']
            self._configure_decoding()
            
        # Update timestamps setting
        if 'compute_timestamps' in config and config['compute_timestamps'] != self.compute_timestamps:
            self.compute_timestamps = config['compute_timestamps']
            self._configure_decoding()
            
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        info = {
            'engine': 'nemo',
            'model': self.model_name,
            'device': self.device,
            'decoder_type': self.decoder_type,
            'compute_timestamps': self.compute_timestamps,
            'batch_size': self.batch_size,
            'stream_chunk_duration': self.stream_chunk_duration,
            'available': PARAKEET_AVAILABLE and self.model is not None
        }
        
        if self.model:
            info['model_type'] = type(self.model).__name__
            # Add vocabulary size if available
            if hasattr(self.model, 'decoder') and hasattr(self.model.decoder, 'vocabulary'):
                info['vocabulary_size'] = len(self.model.decoder.vocabulary)
                
        return info
        
    def _create_temp_file(self):
        """Create a reusable temporary file for audio processing"""
        if self._temp_file_path is None:
            tmp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            self._temp_file_path = tmp_file.name
            tmp_file.close()
            
    def _prepare_audio_file(self, audio_data: bytes, sample_rate: int, 
                           channels: int, sample_width: int) -> str:
        """Prepare audio data as temporary WAV file for NeMo
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            
        Returns:
            Path to temporary WAV file
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
        
        # Handle multi-channel audio
        if channels > 1:
            audio_array = audio_array.reshape(-1, channels).mean(axis=1).astype(dtype)
            
        # Reuse the same temp file path for better performance
        if self._temp_file_path is None:
            self._create_temp_file()
            
        # Write WAV file (overwrites existing content)
        with wave.open(self._temp_file_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_array.tobytes())
            
        return self._temp_file_path
        
    def _is_valid_audio(self, audio_data: bytes, sample_rate: int, 
                       channels: int, sample_width: int) -> bool:
        """Check if audio contains valid speech content
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels  
            sample_width: Sample width in bytes
            
        Returns:
            True if audio seems to contain speech
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
                return False
                
            audio_array = np.frombuffer(audio_data, dtype=dtype)
            
            # Convert to mono if needed
            if channels > 1:
                audio_array = audio_array.reshape(-1, channels).mean(axis=1)
                
            # Convert to float [-1, 1]
            audio_float = audio_array.astype(np.float32) / max_val
            
            # Check duration
            duration = len(audio_float) / sample_rate
            print(f"[ParakeetRecognizer] Audio duration: {duration:.2f}s (min: {self.min_audio_duration}s)")
            if duration < self.min_audio_duration:
                print(f"[ParakeetRecognizer] Audio too short: {duration:.2f}s < {self.min_audio_duration}s")
                return False
            
            # Check energy level
            rms_energy = np.sqrt(np.mean(audio_float ** 2))
            print(f"[ParakeetRecognizer] Audio RMS energy: {rms_energy:.6f} (min: {self.energy_threshold})")
            if rms_energy < self.energy_threshold:
                print(f"[ParakeetRecognizer] Audio energy too low: {rms_energy:.6f} < {self.energy_threshold}")
                return False
            
            # Check silence ratio with more lenient calculation
            silence_threshold = max(rms_energy * 0.2, self.energy_threshold * 0.5)  # More lenient
            silence_samples = np.sum(np.abs(audio_float) < silence_threshold)
            silence_ratio = silence_samples / len(audio_float)
            
            print(f"[ParakeetRecognizer] Silence ratio: {silence_ratio:.2f} (max: {self.silence_ratio_threshold})")
            if silence_ratio > self.silence_ratio_threshold:
                print(f"[ParakeetRecognizer] Too much silence: {silence_ratio:.2f} > {self.silence_ratio_threshold}")
                return False
                
            print(f"[ParakeetRecognizer] ✅ Audio validation passed: duration={duration:.2f}s, energy={rms_energy:.6f}, silence={silence_ratio:.2f}")
            return True
            
        except Exception as e:
            print(f"[ParakeetRecognizer] Audio validation error: {e}")
            return False
            
    def _is_hallucination_text(self, text: str) -> bool:
        """Check if recognized text is likely a hallucination
        
        Args:
            text: Recognized text
            
        Returns:
            True if likely a hallucination
        """
        if not text or len(text.strip()) == 0:
            return True
            
        text = text.strip()
        
        # Common NeMo Japanese hallucinations (only obvious ones)
        nemo_hallucinations = [
            'ピッ', 'ピッ。', 'ピー', 'ピー。',
            'プッ', 'プッ。', 
            '無音', '音なし', 'ノイズ', '雑音',
            # Remove legitimate sounds that might be real speech
        ]
        
        # Check for exact matches (complete hallucinations)
        if text in nemo_hallucinations:
            print(f"[ParakeetRecognizer] Known hallucination detected: '{text}'")
            return True
            
        # Only check for obviously problematic single characters (not real words)
        problematic_single_chars = ['っ', 'ー', '。', '、']
        if len(text) == 1 and text in problematic_single_chars:
            print(f"[ParakeetRecognizer] Problematic single character: '{text}'")
            return True
            
        # Only flag very obvious repeated patterns
        if len(text) <= 2 and len(set(text)) == 1 and text[0] in 'っーピプカタ':
            print(f"[ParakeetRecognizer] Repeated problematic character: '{text}'")
            return True
            
        return False
        
    def _clean_hallucination_prefix(self, text: str) -> str:
        """Remove hallucination prefixes while keeping the actual content
        
        Args:
            text: Recognized text that may contain hallucination prefixes
            
        Returns:
            Cleaned text with hallucination prefixes removed
        """
        if not text:
            return text
            
        original_text = text
        
        # List of hallucination prefixes to remove
        hallucination_prefixes = [
            '心の声', '心の声。', '心の声：', '心の声、', '心の声 ',
            '心の声：「', '心の声:「', '心の声「',
            'ナレーション', 'ナレーション：', 'ナレーション。',
            'モノローグ', 'モノローグ：', 'モノローグ。',
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
                    
                print(f"[ParakeetRecognizer] Removed hallucination prefix '{prefix}' from '{original_text}' → '{text}'")
                break
                
        return text
        
    def recognize(self, 
                  audio_data: bytes, 
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,  # Not used by NeMo
                  prompt: Optional[str] = None) -> Optional[str]:  # Not used by NeMo
        """Recognize speech from audio data
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            language: Language code (ignored, uses model's language)
            prompt: Optional prompt (not used by NeMo)
            
        Returns:
            Recognized text or None if failed
        """
        if not self.model:
            print("[ParakeetRecognizer] Model not initialized")
            return None
            
        # Audio validation is now handled by SpeechRecognitionManager
            
        tmp_path = None
        try:
            # Prepare audio file
            tmp_path = self._prepare_audio_file(audio_data, sample_rate, channels, sample_width)
            
            # Transcribe
            if self.compute_timestamps:
                # Transcribe with timestamps
                hypotheses = self.model.transcribe(
                    [tmp_path], 
                    batch_size=self.batch_size,
                    return_hypotheses=True
                )
                
                if hypotheses and len(hypotheses) > 0:
                    # Extract text from Hypothesis object
                    if hasattr(hypotheses[0], 'text'):
                        result = hypotheses[0].text
                    else:
                        result = str(hypotheses[0])
                    
                    # Log timestamp info if available
                    if hasattr(hypotheses[0], 'timestamp'):
                        print(f"[ParakeetRecognizer] Timestamps available: {hypotheses[0].timestamp}")
                        
                    print(f"[ParakeetRecognizer] Transcription: '{result}'")
                    
                    # Post-processing is now handled by SpeechRecognitionManager
                    return result
            else:
                # Simple transcription - returns list of Hypothesis objects
                transcriptions = self.model.transcribe(
                    [tmp_path], 
                    batch_size=self.batch_size,
                    return_hypotheses=True  # Always return hypotheses to handle properly
                )
                
                if transcriptions and len(transcriptions) > 0:
                    # Extract text from Hypothesis object
                    hypothesis = transcriptions[0]
                    if hasattr(hypothesis, 'text'):
                        result = hypothesis.text
                    else:
                        # Fallback: convert to string and try to extract text
                        result = str(hypothesis)
                        if "text='" in result:
                            start = result.find("text='") + 6
                            end = result.find("'", start)
                            result = result[start:end]
                    
                    print(f"[ParakeetRecognizer] Transcription: '{result}'")
                    
                    # Post-processing is now handled by SpeechRecognitionManager
                    return result
                    
            return None
                    
        except Exception as e:
            print(f"[ParakeetRecognizer] Recognition error: {type(e).__name__}: {e}")
            return None
            
        finally:
            # Don't delete the temp file - we're reusing it
            pass
                    
    def start_stream(self):
        """Start a new streaming session
        
        Note: NeMo doesn't have built-in streaming like some other engines,
        so we implement a buffered approach
        """
        self.stream_buffer = []
        self.stream_sample_rate = 16000
        self.stream_channels = 1
        self.stream_sample_width = 2
        print("[ParakeetRecognizer] Started buffered streaming session")
        
    def process_audio_chunk(self, 
                           audio_data: bytes,
                           sample_rate: int = 16000,
                           channels: int = 1,
                           sample_width: int = 2) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """Process audio chunk for streaming recognition
        
        Since NeMo doesn't support true streaming, we implement
        a buffered approach that processes chunks when they reach
        a certain duration.
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio  
            channels: Number of audio channels
            sample_width: Sample width in bytes
            
        Yields:
            Tuple of (is_final, text) where is_final indicates if the segment is complete
        """
        if not hasattr(self, 'stream_buffer'):
            self.start_stream()
            
        # Store stream parameters
        self.stream_sample_rate = sample_rate
        self.stream_channels = channels
        self.stream_sample_width = sample_width
            
        # Add to buffer
        self.stream_buffer.append(audio_data)
        
        # Calculate buffer duration
        buffer_size = sum(len(chunk) for chunk in self.stream_buffer)
        chunk_duration = buffer_size / (sample_rate * channels * sample_width)
        
        # Process when buffer reaches threshold duration
        if chunk_duration >= self.stream_chunk_duration:
            # Combine buffer
            combined_audio = b''.join(self.stream_buffer)
            
            # Process the audio
            text = self.recognize(
                combined_audio, 
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width
            )
            
            if text:
                # For NeMo, results are always final since we process in chunks
                yield (True, text)
                
            # Clear buffer for next chunk
            self.stream_buffer = []
            
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and process remaining audio
        
        Returns:
            Final transcription text or None
        """
        if not hasattr(self, 'stream_buffer') or not self.stream_buffer:
            return None
            
        # Process remaining buffer
        combined_audio = b''.join(self.stream_buffer)
        self.stream_buffer = []
        
        # Only process if there's meaningful audio
        buffer_duration = len(combined_audio) / (
            self.stream_sample_rate * self.stream_channels * self.stream_sample_width
        )
        
        if buffer_duration < 0.1:  # Skip very short audio
            return None
        
        return self.recognize(
            combined_audio, 
            sample_rate=self.stream_sample_rate,
            channels=self.stream_channels,
            sample_width=self.stream_sample_width
        )
        
    def __del__(self):
        """Cleanup temporary file on deletion"""
        if hasattr(self, '_temp_file_path') and self._temp_file_path and os.path.exists(self._temp_file_path):
            try:
                os.unlink(self._temp_file_path)
            except:
                pass