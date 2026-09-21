"""
Google Cloud Speech-to-Text recognition implementation
"""
import io
import inspect
import numpy as np
from collections.abc import Mapping
from typing import Optional, Generator, Tuple, Dict, Any

from src.services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
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
        privacy_config = self.config
        # SpeechRecognitionManager passes the engine-local settings mapping,
        # which may not contain application-wide external_model_privacy.
        # Resolve the DB-backed application config instead of silently
        # selecting the gateway's historical direct-mode default.
        if not isinstance(privacy_config, Mapping) or "external_model_privacy" not in privacy_config:
            try:
                from src.config import Config

                privacy_config = Config()
            except Exception as exc:
                raise PrivacyError(
                    "Google Speech privacy configuration is unavailable"
                ) from exc
        return OutboundPrivacyGateway(
            privacy_config,
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
        # Never fall back to the unreviewed microphone buffer when the
        # protection result is malformed or omitted.  This helper remains for
        # compatibility with callers/tests that only perform a preflight; all
        # real SDK sends use ``_execute_sync`` below.
        raise PrivacyError("Google Speech privacy protection returned no audio")

    def _descriptor(self, *, action: str) -> EgressDescriptor:
        """Describe one Google Cloud Speech transaction for audit/review."""

        return EgressDescriptor(
            action=action,
            transport="google.cloud.speech",
            destination="https://speech.googleapis.com",
            provider="google_speech",
            tool="speech_recognition.google",
            model=self.model,
        )

    def _execute_sync(self, payload: Any, *, action: str, sender) -> Any:
        """Run a complete SDK call behind the outbound privacy gateway.

        The sender receives the approved payload and is the sole owner of the
        Google SDK transport.  In particular, it never falls back to the raw
        audio captured by the recognizer when review/redaction fails.
        """

        return self._privacy_gateway().execute_sync(
            payload,
            provider="google_speech",
            descriptor=self._descriptor(action=action),
            sender=sender,
            base_url="https://speech.googleapis.com",
            source_kind=action,
            model=self.model,
        )

    @staticmethod
    def _invoke_without_retry(method, *args: Any, **kwargs: Any) -> Any:
        """Invoke a Google RPC with retries disabled when the API supports it.

        Generated Google clients expose a ``retry`` keyword.  Small test or
        deployment fakes often expose only ``(*args, **kwargs)`` or a narrow
        signature; inspect first so compatibility does not require catching a
        TypeError and replaying the request a second time.
        """

        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "retry" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs.setdefault("retry", None)
        return method(*args, **kwargs)
        
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
                batch_audio = b"".join(
                    bytes(getattr(item, "audio_content", b"") or b"")
                    for item in self.stream_requests
                )

                def send_stream(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError("Google Speech streaming payload is malformed")
                    outbound_audio = protected_payload.get("audio")
                    if not isinstance(outbound_audio, (bytes, bytearray)):
                        raise PrivacyError("Google Speech streaming audio is unavailable")
                    requests = iter(
                        [
                            speech.StreamingRecognizeRequest(
                                streaming_config=protected_payload.get(
                                    "streaming_config", self.streaming_config
                                )
                            )
                        ]
                        + [
                            speech.StreamingRecognizeRequest(
                                audio_content=bytes(outbound_audio)
                            )
                        ]
                    )
                    # Explicitly disable client-library retries.  A streaming
                    # request is one audited transaction and must not be
                    # replayed behind the gateway.
                    return self._invoke_without_retry(
                        self.client.streaming_recognize,
                        requests,
                    )

                # The complete batch (config + audio) is the reviewed
                # transaction.  No SDK request is opened before approval.
                responses = self._execute_sync(
                    {
                        "streaming_config": self.streaming_config,
                        "audio": batch_audio,
                    },
                    action="google_speech_streaming",
                    sender=send_stream,
                )
                
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
            batch_audio = b"".join(
                bytes(getattr(item, "audio_content", b"") or b"")
                for item in self.stream_requests
            )

            def send_stream(protected_payload: Any):
                if not isinstance(protected_payload, Mapping):
                    raise PrivacyError("Google Speech final payload is malformed")
                outbound_audio = protected_payload.get("audio")
                if not isinstance(outbound_audio, (bytes, bytearray)):
                    raise PrivacyError("Google Speech final audio is unavailable")
                requests = iter(
                    [
                        speech.StreamingRecognizeRequest(
                            streaming_config=protected_payload.get(
                                "streaming_config", self.streaming_config
                            )
                        )
                    ]
                    + [
                        speech.StreamingRecognizeRequest(
                            audio_content=bytes(outbound_audio)
                        )
                    ]
                )
                return self._invoke_without_retry(
                    self.client.streaming_recognize,
                    requests,
                )

            responses = self._execute_sync(
                {
                    "streaming_config": self.streaming_config,
                    "audio": batch_audio,
                },
                action="google_speech_streaming_final",
                sender=send_stream,
            )
            
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
            
            # Configure recognition
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.sample_rate,
                language_code=language or self.language,
                enable_automatic_punctuation=self.enable_automatic_punctuation,
                model=self.model,
            )
            
            def send_recognition(protected_payload: Any):
                if not isinstance(protected_payload, Mapping):
                    raise PrivacyError("Google Speech recognition payload is malformed")
                outbound_audio = protected_payload.get("audio")
                if not isinstance(outbound_audio, (bytes, bytearray)):
                    raise PrivacyError("Google Speech recognition audio is unavailable")
                audio = speech.RecognitionAudio(content=bytes(outbound_audio))
                # Explicit retry=None prevents a failed request from silently
                # replaying user audio outside this transaction boundary.
                return self._invoke_without_retry(
                    self.client.recognize,
                    config=protected_payload.get("config", config),
                    audio=audio,
                )

            response = self._execute_sync(
                {"config": config, "audio": audio_data},
                action="google_speech_recognize",
                sender=send_recognition,
            )
            
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
