"""
Google Gemini speech recognition implementation
"""
import io
import time
import wave
from typing import Optional, Generator, Tuple, Dict, Any, Mapping
from collections import deque

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("[GeminiRecognizer] google-generativeai not available")

from ..base import SpeechRecognizerInterface
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    effective_privacy_mode,
    get_privacy_policy_context,
    privacy_config,
)

def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy usage persistence import for optional audio deployments."""
    from ...llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

class GeminiSpeechRecognizer(SpeechRecognizerInterface):
    """Google Gemini speech recognition implementation"""
    
    def __init__(
        self,
        config: Dict[str, Any] = None,
        usage_client: Any = None,
        usage_context: Any = None,
    ):
        """Initialize Gemini speech recognizer
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        self.api_key = self.config.get('api_key')
        self.model_name = self.config.get('model', 'gemini-3-flash-preview')
        self.language = self.config.get('language', 'ja')
        self.chunk_length = self.config.get('chunk_length', 3.0)
        self.sample_rate = 16000
        
        # Initialize Gemini
        self.model = None
        if GEMINI_AVAILABLE and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
                print(f"[GeminiRecognizer] Initialized with model '{self.model_name}'")
            except Exception as e:
                print(f"[GeminiRecognizer] Failed to initialize: {type(e).__name__}: {e}")
        else:
            if not GEMINI_AVAILABLE:
                print("[GeminiRecognizer] google-generativeai is not installed")
            if not self.api_key:
                print("[GeminiRecognizer] API key not provided")
                
        # Buffer for streaming mode
        self.audio_buffer = deque()
        self.buffer_duration = 0.0

        # A Gemini response is normally consumed immediately, but keeping a
        # small identity-based ledger prevents an adapter/caller from asking
        # us to persist the same response twice.  Do not key this by ``id``:
        # response objects may be short-lived and Python can reuse ids.
        self._recorded_usage_responses = []
        # These are immutable references used only as defaults.  A request
        # may pass an explicit context to ``recognize``/stream methods without
        # mutating shared recognizer state.
        self.usage_client = usage_client
        self.usage_context = usage_context
        self._privacy_gateway = OutboundPrivacyGateway(
            getattr(usage_client, "config", None) or self.config,
        )
        self._sync_privacy_gateway(usage_client=usage_client, usage_context=usage_context)
        self._stream_usage_client = None
        self._stream_usage_context = None

    @staticmethod
    def _context_value(context: Any, name: str, *aliases: str) -> Any:
        keys = (name, *aliases)
        if isinstance(context, Mapping):
            for key in keys:
                value = context.get(key)
                if value is not None:
                    return value
        for key in keys:
            value = getattr(context, key, None)
            if value is not None:
                return value
        return None

    def _sync_privacy_gateway(
        self,
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> None:
        """Refresh request identity/policy before each Gemini upload."""

        candidate = usage_client or usage_context or self.usage_client or self.usage_context
        if usage_client is None and usage_context is None:
            try:
                from ...services.turn_context import get_turn_context

                turn = get_turn_context()
            except Exception:
                turn = None
            if turn is not None and any(
                getattr(turn, field, None)
                for field in ("user_id", "session_id", "project_id")
            ):
                candidate = turn
        config = self._context_value(candidate, "config") or self.config
        if config is not self._privacy_gateway.config:
            self._privacy_gateway.config = config
        user_id = self._context_value(candidate, "user_id", "session_user_id")
        if user_id is None:
            getter = getattr(candidate, "_get_session_user_id", None)
            if callable(getter):
                try:
                    user_id = getter()
                except Exception:
                    user_id = None
        session_id = self._context_value(candidate, "current_session_id", "session_id")
        previous_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        self._privacy_gateway.user_id = str(user_id or "")
        self._privacy_gateway.session_id = str(session_id or "")
        if previous_identity != (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        ):
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()

        inherited = get_privacy_policy_context()
        session_policy = self._context_value(candidate, "session_context", "privacy_context")
        project_metadata = self._context_value(
            candidate,
            "project_metadata",
            "project_metadata_context",
        )
        if not isinstance(session_policy, Mapping):
            session_policy = inherited.session_context
        if not isinstance(project_metadata, Mapping):
            project_metadata = inherited.project_metadata
        privacy_mode = self._context_value(candidate, "privacy_mode")
        if privacy_mode is not None:
            session_policy = dict(session_policy or {})
            session_policy["privacy_mode"] = str(privacy_mode or "")
        self._privacy_gateway.update_policy_context(
            session_context=dict(session_policy or {}),
            project_metadata=dict(project_metadata or {}),
        )
        settings = privacy_config(config)
        self._privacy_gateway.settings = settings.__class__(
            **{
                **settings.__dict__,
                "mode": effective_privacy_mode(
                    config,
                    session_context=self._privacy_gateway.session_context,
                    project_metadata=self._privacy_gateway.project_metadata,
                ),
            }
        )

    def set_usage_context(
        self,
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> None:
        """Set optional default usage context for manager-created recognizers.

        The manager uses this only for a recognizer-scoped default.  Callers
        handling concurrent requests should pass ``usage_client`` or
        ``usage_context`` to the individual recognition method instead.
        """
        self.usage_client = usage_client
        self.usage_context = usage_context
        self._sync_privacy_gateway(usage_client=usage_client, usage_context=usage_context)

    @staticmethod
    def _usage_client_from_context(context: Any) -> Any:
        """Adapt a request context into the client shape expected by usage DB."""
        if context is None:
            return None
        if (
            hasattr(context, "current_session_id")
            or hasattr(context, "current_project_id")
            or callable(getattr(context, "_get_session_user_id", None))
        ):
            return context
        try:
            from types import SimpleNamespace

            def value(name: str, *aliases: str) -> Any:
                if isinstance(context, dict):
                    for key in (name, *aliases):
                        item = context.get(key)
                        if item is not None:
                            return item
                for key in (name, *aliases):
                    item = getattr(context, key, None)
                    if item is not None:
                        return item
                return None

            user_id = value("user_id")
            return SimpleNamespace(
                current_session_id=value("current_session_id", "session_id"),
                current_project_id=value("current_project_id", "project_id"),
                character_name=value("character_name"),
                _get_session_user_id=lambda: user_id,
            )
        except Exception:
            return None

    def _resolve_usage_client(
        self,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> Any:
        if usage_client is not None:
            return usage_client
        if usage_context is not None:
            return self._usage_client_from_context(usage_context)
        if getattr(self, "usage_client", None) is not None:
            return self.usage_client
        if getattr(self, "usage_context", None) is not None:
            return self._usage_client_from_context(self.usage_context)
        return None

    @staticmethod
    def _usage_value(usage: Any, name: str) -> Any:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        return value

    @classmethod
    def _gemini_usage_payload(cls, response: Any) -> Optional[Dict[str, Any]]:
        """Convert Gemini ``usage_metadata`` to the usage ledger shape.

        Gemini's generative API does not expose OpenAI's ``usage`` object;
        token counts are nested under ``usage_metadata`` instead.  Returning
        ``None`` when both token counters are absent is intentional: callers
        must not manufacture an STT cost when a provider did not report one.
        """
        usage = getattr(response, "usage_metadata", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage_metadata")
        if usage is None:
            return None

        def count(name: str) -> Optional[int]:
            value = cls._usage_value(usage, name)
            if value is None:
                return None
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return None

        input_tokens = count("prompt_token_count")
        output_tokens = count("candidates_token_count")
        if input_tokens is None and output_tokens is None:
            return None

        cached_tokens = count("cached_content_token_count") or 0
        payload: Dict[str, Any] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cached_tokens": cached_tokens,
            "cache_read_tokens": cached_tokens,
            "reasoning_tokens": count("thoughts_token_count") or 0,
            "cache_provider": "gemini",
            "metrics_source": "gemini.usage_metadata",
        }
        resolved_model = getattr(response, "model_version", None)
        if resolved_model is None and isinstance(response, dict):
            resolved_model = response.get("model_version")
        if resolved_model:
            payload["resolved_model"] = str(resolved_model)
        return payload

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Return ``True`` if this exact response was already persisted."""
        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            # Some SDK response objects are slotted/frozen.  Keep a bounded
            # strong-reference list in that case so object identity remains
            # stable even when Python reuses object ids.
            recorded = getattr(self, "_recorded_usage_responses", None)
            if recorded is None:
                recorded = []
                self._recorded_usage_responses = recorded
            if any(item is response for item in recorded):
                return True
            recorded.append(response)
            del recorded[:-8]
            return False

    def _record_gemini_usage(
        self,
        response: Any,
        *,
        latency_ms: int = 0,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> None:
        """Persist one successful Gemini STT response when usage is reported."""
        try:
            payload = self._gemini_usage_payload(response)
            if not payload or self._mark_usage_recorded(response):
                return
            # A recognizer normally has no LLM client/session object.  Tests
            # and Discord integrations may attach one as ``usage_client``;
            # passing it through preserves user/session/project context when
            # available while still allowing standalone STT accounting.
            resolved_usage_client = self._resolve_usage_client(
                usage_client,
                usage_context,
            )
            if resolved_usage_client is None:
                try:
                    from types import SimpleNamespace

                    from ...services.turn_context import get_turn_context

                    turn = get_turn_context()
                    resolved_usage_client = SimpleNamespace(
                        current_session_id=turn.session_id,
                        current_project_id=turn.project_id,
                        character_name=None,
                        _get_session_user_id=lambda: turn.user_id,
                    )
                except Exception:
                    resolved_usage_client = None
            persist_usage_sync(
                resolved_usage_client,
                provider="gemini",
                model=str(self.model_name),
                usage=payload,
                request_type="stt",
                latency_ms=max(int(latency_ms or 0), 0),
            )
        except Exception as exc:  # pragma: no cover - defensive telemetry path
            print(f"[GeminiRecognizer] usage記録に失敗しました: {exc}")
        
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        self.config.update(config)
        
        # Update API key if changed
        new_api_key = self.config.get('api_key')
        if new_api_key != self.api_key:
            self.api_key = new_api_key
            if GEMINI_AVAILABLE and self.api_key:
                try:
                    genai.configure(api_key=self.api_key)
                    print("[GeminiRecognizer] Updated API key")
                except Exception as e:
                    print(f"[GeminiRecognizer] Failed to update API key: {e}")
        
        # Update other configuration
        self.model_name = self.config.get('model', self.model_name)
        self.language = self.config.get('language', self.language)
        self.chunk_length = self.config.get('chunk_length', self.chunk_length)
        self._sync_privacy_gateway()
        
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        return {
            'engine': 'gemini',
            'model': self.model_name,
            'language': self.language,
            'chunk_length': self.chunk_length,
            'gemini_available': GEMINI_AVAILABLE,
            'api_key_configured': bool(self.api_key),
            'model_initialized': self.model is not None
        }
        
    def start_stream(
        self,
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ):
        """Start a new streaming session"""
        self.audio_buffer.clear()
        self.buffer_duration = 0.0
        # Stream-scoped defaults avoid requiring callers to repeat context on
        # every chunk while keeping request context separate from other
        # recognizer instances.
        self._stream_usage_client = usage_client
        self._stream_usage_context = usage_context
        print("[GeminiRecognizer] Started new streaming session")
        
    def process_audio_chunk(self,
                           audio_data: bytes,
                           sample_rate: int = 16000,
                           channels: int = 1,
                           sample_width: int = 2,
                           *,
                           usage_client: Any = None,
                           usage_context: Any = None) -> Generator[Tuple[bool, Optional[str]], None, None]:
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
            
        # Add to buffer
        self.audio_buffer.append(audio_data)
        chunk_duration = len(audio_data) / (sample_rate * channels * sample_width)
        self.buffer_duration += chunk_duration
        
        # Process if we have enough audio
        if self.buffer_duration >= self.chunk_length:
            # Combine all chunks in buffer
            combined_audio = b''.join(self.audio_buffer)
            
            # Clear buffer
            self.audio_buffer.clear()
            self.buffer_duration = 0.0
            
            # Transcribe the chunk
            result = self.recognize(
                combined_audio,
                sample_rate,
                channels,
                sample_width,
                usage_client=(
                    usage_client
                    if usage_client is not None
                    else self._stream_usage_client
                ),
                usage_context=(
                    usage_context
                    if usage_context is not None
                    else self._stream_usage_context
                ),
            )
            
            if result:
                yield (True, result)
                
    def finish_stream(
        self,
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> Optional[str]:
        """Finish streaming and process remaining audio
        
        Returns:
            Final transcription text or None
        """
        if not self.model or len(self.audio_buffer) == 0:
            return None
            
        # Process remaining audio in buffer
        combined_audio = b''.join(self.audio_buffer)
        
        # Clear buffer
        self.audio_buffer.clear()
        self.buffer_duration = 0.0
        
        return self.recognize(
            combined_audio,
            usage_client=(
                usage_client
                if usage_client is not None
                else self._stream_usage_client
            ),
            usage_context=(
                usage_context
                if usage_context is not None
                else self._stream_usage_context
            ),
        )
    
    def recognize(self,
                  audio_data: bytes,
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,
                  prompt: Optional[str] = None,
                  *,
                  usage_client: Any = None,
                  usage_context: Any = None) -> Optional[str]:
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
        if not self.model:
            print("[GeminiRecognizer] Gemini model not initialized")
            return None
            
        started = time.monotonic()
        try:
            self._sync_privacy_gateway(
                usage_client=usage_client,
                usage_context=usage_context,
            )
            # Create WAV file from audio data
            wav_data = self._create_wav_data(audio_data, sample_rate, channels, sample_width)
            
            # Create a file-like object
            audio_file = io.BytesIO(wav_data)
            audio_file.name = "audio.wav"
            
            # Prepare the prompt with strict instructions to reduce hallucinations
            lang = language or self.language
            base_prompt = f"""Please transcribe this audio to text in {lang}. 

IMPORTANT INSTRUCTIONS:
- Only transcribe actual speech that you can clearly hear
- Do NOT add any sound effects, beeping sounds, or noise descriptions
- Do NOT transcribe repetitive patterns like "ピッピッピッ" or "beep beep" 
- If the audio contains only noise or unclear sounds, respond with "[UNCLEAR]"
- If there is some speech followed by noise, only transcribe the clear speech part
- Focus on real human conversation, ignore background noise"""
            
            if prompt:
                base_prompt += f" Context: {prompt}"

            protected = self._privacy_gateway.protect_sync(
                {"prompt": base_prompt, "media": wav_data},
                provider="gemini",
                source_kind="audio_transcription",
            )
            if isinstance(protected.payload, dict):
                base_prompt = str(protected.payload.get("prompt") or base_prompt)
            
            # Upload and transcribe
            uploaded_file = genai.upload_file(audio_file, mime_type="audio/wav")
            
            try:
                response = self.model.generate_content([
                    base_prompt,
                    uploaded_file
                ])
                
                if response.text:
                    result = response.text.strip()
                    print(f"[GeminiRecognizer] Transcription result: '{result}'")
                    
                    # Handle [UNCLEAR] responses
                    if "[UNCLEAR]" in result:
                        print(f"[GeminiRecognizer] Geminiが不明瞭と判定: '{result}'")
                        return None
                    
                    # Apply Gemini-specific hallucination filtering and cleaning
                    cleaned_result = self._clean_gemini_hallucinations(result)
                    if not cleaned_result:
                        print(f"[GeminiRecognizer] Gemini幻聴を検出してフィルタ: '{result}'")
                        return None
                    
                    if cleaned_result != result:
                        print(f"[GeminiRecognizer] 幻聴部分を除去: '{result}' → '{cleaned_result}'")

                    # Only successful transcription responses reach this
                    # point.  If Gemini omitted usage_metadata the helper is a
                    # no-op; estimated tokens are never fabricated.
                    self._record_gemini_usage(
                        response,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        usage_client=usage_client,
                        usage_context=usage_context,
                    )

                    return cleaned_result
                else:
                    print("[GeminiRecognizer] No transcription result")
                    return None
                    
            finally:
                # Clean up uploaded file
                try:
                    uploaded_file.delete()
                except:
                    pass
                    
        except Exception as e:
            print(f"[GeminiRecognizer] Recognition error: {type(e).__name__}: {e}")
            return None
            
    def _create_wav_data(self, 
                        audio_data: bytes, 
                        sample_rate: int, 
                        channels: int, 
                        sample_width: int) -> bytes:
        """Create WAV format data from raw audio
        
        Args:
            audio_data: Raw audio data
            sample_rate: Sample rate
            channels: Number of channels
            sample_width: Sample width in bytes
            
        Returns:
            WAV format audio data
        """
        # Create WAV file in memory
        wav_buffer = io.BytesIO()
        
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)
            
        wav_buffer.seek(0)
        return wav_buffer.read()
    
    def _is_gemini_hallucination(self, text: str) -> bool:
        """Check if text contains Gemini-specific hallucination patterns
        
        Args:
            text: Transcribed text to check
            
        Returns:
            True if likely a Gemini hallucination
        """
        if not text:
            return True
            
        # Remove whitespace and newlines for analysis
        clean_text = ''.join(text.split())
        
        # Check for repetitive "ピッ" patterns (common Gemini hallucination)
        pip_count = clean_text.count('ピッ')
        if pip_count >= 5:  # Require more repetitions (was 3, now 5)
            # Check if it dominates the text
            pip_ratio = (pip_count * 2) / len(clean_text) if len(clean_text) > 0 else 0
            if pip_ratio > 0.5:  # Require higher dominance (was 30%, now 50%)
                return True
                
        # Check for other Gemini-specific patterns
        gemini_hallucination_patterns = [
            'ピッピッピッピッピッピッ',  # 6+ consecutive
            'プップップップップップップ',
            'ブーブーブーブーブーブー',
            'ビープビープビープビープ',
            'ピーピーピーピーピーピー',
        ]
        
        for pattern in gemini_hallucination_patterns:
            if pattern in clean_text:
                return True
                
        # Check for mixed real speech with trailing noise patterns
        lines = text.strip().split('\n')
        if len(lines) >= 2:
            last_line = lines[-1].strip()
            # If last line is pure repetitive noise after real speech
            if len(last_line) >= 6 and ('ピッ' in last_line or 'ピー' in last_line):
                # Check if last line is mostly noise
                noise_chars = last_line.count('ピ') + last_line.count('プ') + last_line.count('ブ')
                if noise_chars / len(last_line) > 0.7:  # 70% noise characters
                    # Remove just the noisy last line, keep the real speech
                    remaining_text = '\n'.join(lines[:-1]).strip()
                    if len(remaining_text) > 10:  # If there's substantial real content
                        # Update the original text reference (this is a limitation)
                        # For now, we'll flag as hallucination to be safe
                        return True
                        
        # Check for pure noise without real content (more lenient)
        real_japanese_chars = sum(1 for c in text if c in 'あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん')
        noise_chars = text.count('ピ') + text.count('プ') + text.count('ブ')
        
        # Only flag if noise clearly dominates and there's substantial noise
        if len(text) > 20 and noise_chars > real_japanese_chars * 2 and noise_chars > 10:
            return True
            
        return False
    
    def _clean_gemini_hallucinations(self, text: str) -> str:
        """Clean Gemini hallucinations while preserving real speech
        
        Args:
            text: Original transcribed text
            
        Returns:
            Cleaned text with hallucinations removed, or empty string if pure hallucination
        """
        if not text:
            return ""
            
        # First check if it's pure hallucination (but be more lenient)
        # Only reject if it's clearly pure noise
        lines = text.strip().split('\n')
        total_chars = len(''.join(lines))
        noise_chars = sum(line.count('ピ') + line.count('プ') + line.count('ブ') for line in lines)
        
        if total_chars > 0 and noise_chars / total_chars > 0.8:  # 80% noise
            return ""
            
        # Try to clean by removing trailing noise patterns
        lines = text.strip().split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Check if this line is mostly noise
            noise_chars = line.count('ピ') + line.count('プ') + line.count('ブ')
            if len(line) > 6 and noise_chars / len(line) > 0.7:
                # Skip this noisy line
                print(f"[GeminiRecognizer] ノイズ行をスキップ: '{line}'")
                continue
                
            # Remove trailing repetitive patterns from the line
            cleaned_line = self._remove_trailing_noise(line)
            if cleaned_line:
                cleaned_lines.append(cleaned_line)
        
        result = '\n'.join(cleaned_lines).strip()
        
        # Final check: if result is too short or still mostly noise, reject
        if len(result) < 3:
            return ""
            
        return result
    
    def _remove_trailing_noise(self, line: str) -> str:
        """Remove trailing noise patterns from a line
        
        Args:
            line: Input line
            
        Returns:
            Line with trailing noise removed
        """
        # Define noise patterns to remove from the end
        noise_patterns = ['ピッ', 'ピー', 'プッ', 'ブー', 'ビープ']
        
        # Keep removing noise patterns from the end
        while True:
            original_line = line
            for pattern in noise_patterns:
                if line.endswith(pattern):
                    line = line[:-len(pattern)].strip()
                    break
                    
            # Also remove if there's repetitive noise at the end
            for pattern in noise_patterns:
                if pattern in line[-10:]:  # Check last 10 characters
                    # Count consecutive patterns at the end
                    count = 0
                    temp_line = line
                    while temp_line.endswith(pattern):
                        temp_line = temp_line[:-len(pattern)]
                        count += 1
                    
                    if count >= 2:  # 2 or more consecutive patterns
                        line = temp_line.strip()
                        break
            
            # If no changes made, break
            if line == original_line:
                break
                
        return line.strip()
