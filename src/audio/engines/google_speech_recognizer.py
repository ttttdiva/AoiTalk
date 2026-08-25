"""
Google Cloud Speech-to-Text recognition implementation
"""
import io
import numpy as np
from typing import Optional, Generator, Tuple, Dict, Any

from src.services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    get_privacy_policy_context,
)
from src.services.turn_context import get_turn_context

try:
    from google.cloud import speech
    GOOGLE_SPEECH_AVAILABLE = True
except ImportError:
    GOOGLE_SPEECH_AVAILABLE = False
    # Moved print to init method to avoid startup noise

from ..base import SpeechRecognizerInterface


class GoogleSpeechRecognizer(SpeechRecognizerInterface):
    """Google Cloud Speech-to-Text recognition implementation"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize Google Speech recognizer
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        
        if not GOOGLE_SPEECH_AVAILABLE:
            raise ImportError("google-cloud-speech is required but not installed")
            
        # Initialize Google Speech client
        try:
            self.client = speech.SpeechClient()
        except Exception as e:
            print(f"[GoogleSpeechRecognizer] Failed to initialize client: {e}")
            raise
            
        # Configuration
        self.language = self.config.get('language', 'ja-JP')
        self.sample_rate = self.config.get('sample_rate', 16000)
        self.enable_automatic_punctuation = self.config.get('enable_automatic_punctuation', True)
        self.model = self.config.get('model', 'latest_long')  # or 'latest_short'
        
        # Streaming configuration
        self.streaming_config = None
        self.stream_requests = []

    def _privacy_gateway(self) -> OutboundPrivacyGateway:
        """Build a request-scoped gateway for direct Google STT transport."""

        try:
            turn = get_turn_context()
        except Exception:
            turn = None
        inherited = get_privacy_policy_context()
        session_context = self.config.get("session_context")
        project_metadata = self.config.get("project_metadata")
        if not isinstance(session_context, dict):
            session_context = inherited.session_context
        if not isinstance(project_metadata, dict):
            project_metadata = inherited.project_metadata
        return OutboundPrivacyGateway(
            self.config,
            user_id=str(
                self.config.get("user_id")
                or getattr(turn, "user_id", None)
                or ""
            ),
            session_id=str(
                self.config.get("session_id")
                or getattr(turn, "session_id", None)
                or ""
            ),
            session_context=session_context if isinstance(session_context, dict) else None,
            project_metadata=project_metadata if isinstance(project_metadata, dict) else None,
        )

    def _gate_audio(self, audio_data: bytes, *, source_kind: str) -> bytes:
        """Apply privacy policy immediately before Google sends raw audio."""

        protected = self._privacy_gateway().protect_sync(
            {"audio": bytes(audio_data or b"")},
            provider="google_speech",
            base_url="https://speech.googleapis.com",
            source_kind=source_kind,
        )
        payload = protected.payload
        if isinstance(payload, dict) and isinstance(payload.get("audio"), (bytes, bytearray)):
            return bytes(payload["audio"])
        return bytes(audio_data or b"")
        
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        self.config.update(config)
        self.language = self.config.get('language', self.language)
        self.sample_rate = self.config.get('sample_rate', self.sample_rate)
        self.enable_automatic_punctuation = self.config.get('enable_automatic_punctuation', self.enable_automatic_punctuation)
        self.model = self.config.get('model', self.model)
        
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        return {
            'engine': 'google',
            'language': self.language,
            'sample_rate': self.sample_rate,
            'model': self.model,
            'automatic_punctuation': self.enable_automatic_punctuation,
            'available': GOOGLE_SPEECH_AVAILABLE
        }
        
    def start_stream(self):
        """Start a new streaming session"""
        # Configure streaming recognition
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.sample_rate,
            language_code=self.language,
            enable_automatic_punctuation=self.enable_automatic_punctuation,
            model=self.model,
        )
        
        self.streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
            single_utterance=False,
        )
        
        self.stream_requests = []
        print("[GoogleSpeechRecognizer] Started new streaming session")
        
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
        if not self.streaming_config:
            print("[GoogleSpeechRecognizer] Streaming not started")
            return
            
        # Convert audio if needed
        if sample_rate != self.sample_rate or channels != 1:
            audio_data = self._convert_audio(audio_data, sample_rate, channels, sample_width)
            
        # Create streaming request
        request = speech.StreamingRecognizeRequest(audio_content=audio_data)
        self.stream_requests.append(request)
        
        # Process accumulated requests
        if len(self.stream_requests) >= 5:  # Process every 5 chunks
            try:
                # Gate the complete batch immediately before opening the
                # Google streaming transport.  Protected/local_only policy
                # therefore fails closed before ``streaming_recognize``.
                batch_audio = b"".join(
                    bytes(getattr(item, "audio_content", b"") or b"")
                    for item in self.stream_requests
                )
                gated_audio = self._gate_audio(
                    batch_audio,
                    source_kind="google_speech_streaming",
                )
                requests = iter(
                    [speech.StreamingRecognizeRequest(streaming_config=self.streaming_config)]
                    + [speech.StreamingRecognizeRequest(audio_content=gated_audio)]
                )
                
                # Get responses
                responses = self.client.streaming_recognize(requests)
                
                for response in responses:
                    for result in response.results:
                        if result.alternatives:
                            text = result.alternatives[0].transcript
                            is_final = result.is_final
                            if text.strip():
                                yield (is_final, text.strip())
                
                # Clear processed requests
                self.stream_requests = []
                
            except Exception as e:
                print(f"[GoogleSpeechRecognizer] Streaming error: {e}")
                self.stream_requests = []
                
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and process remaining audio
        
        Returns:
            Final transcription text or None
        """
        if not self.stream_requests:
            return None
            
        try:
            # Process remaining requests only after the raw-media gate.
            batch_audio = b"".join(
                bytes(getattr(item, "audio_content", b"") or b"")
                for item in self.stream_requests
            )
            gated_audio = self._gate_audio(
                batch_audio,
                source_kind="google_speech_streaming_final",
            )
            requests = iter(
                [speech.StreamingRecognizeRequest(streaming_config=self.streaming_config)]
                + [speech.StreamingRecognizeRequest(audio_content=gated_audio)]
            )
            
            responses = self.client.streaming_recognize(requests)
            
            final_text = ""
            for response in responses:
                for result in response.results:
                    if result.alternatives and result.is_final:
                        final_text += result.alternatives[0].transcript
            
            self.stream_requests = []
            return final_text.strip() if final_text else None
            
        except Exception as e:
            print(f"[GoogleSpeechRecognizer] Final streaming error: {e}")
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
            prompt: Optional prompt to guide recognition (not used in Google Speech)
            
        Returns:
            Recognized text or None if failed
        """
        try:
            # Convert audio if needed
            if sample_rate != self.sample_rate or channels != 1:
                audio_data = self._convert_audio(audio_data, sample_rate, channels, sample_width)
            
            # Create audio object
            audio = speech.RecognitionAudio(content=audio_data)
            
            # Configure recognition
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.sample_rate,
                language_code=language or self.language,
                enable_automatic_punctuation=self.enable_automatic_punctuation,
                model=self.model,
            )
            
            # Perform recognition only after the raw-media gate.
            gated_audio = self._gate_audio(
                audio_data,
                source_kind="google_speech_recognize",
            )
            audio = speech.RecognitionAudio(content=gated_audio)
            response = self.client.recognize(config=config, audio=audio)
            
            # Extract result
            if response.results and response.results[0].alternatives:
                result = response.results[0].alternatives[0].transcript
                print(f"[GoogleSpeechRecognizer] Transcription result: '{result}'")
                return result.strip()
            else:
                return None
                
        except Exception as e:
            print(f"[GoogleSpeechRecognizer] Recognition error: {type(e).__name__}: {e}")
            return None
            
    def _convert_audio(self, 
                      audio_data: bytes, 
                      sample_rate: int, 
                      channels: int, 
                      sample_width: int) -> bytes:
        """Convert audio to the format expected by Google Speech
        
        Args:
            audio_data: Raw audio data
            sample_rate: Source sample rate
            channels: Number of channels
            sample_width: Sample width in bytes
            
        Returns:
            Converted audio data
        """
        # Convert to numpy for processing
        if sample_width == 1:
            dtype = np.int8
        elif sample_width == 2:
            dtype = np.int16
        elif sample_width == 4:
            dtype = np.int32
        else:
            raise ValueError(f"Unsupported sample width: {sample_width}")
            
        audio_array = np.frombuffer(audio_data, dtype=dtype)
        
        # Convert to mono if needed
        if channels > 1:
            audio_array = audio_array.reshape(-1, channels).mean(axis=1).astype(dtype)
        
        # Resample if needed
        if sample_rate != self.sample_rate:
            try:
                import scipy.signal
                duration = len(audio_array) / sample_rate
                target_samples = int(duration * self.sample_rate)
                audio_array = scipy.signal.resample(audio_array, target_samples).astype(dtype)
            except ImportError:
                print("[GoogleSpeechRecognizer] scipy not available, skipping resampling")
        
        return audio_array.tobytes()
