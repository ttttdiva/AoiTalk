"""
Nijivoice TTS engine implementation
"""
import asyncio
import os
from typing import Optional, List, Dict, Any
import httpx
import json
import io
from collections.abc import Mapping
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
try:
    from pydub import AudioSegment
except ImportError:
    # Audio conversion is optional for the core/Linux build.  The engine can
    # still be imported and will report an explicit synthesis failure if the
    # audio extra is selected without pydub/ffmpeg.
    AudioSegment = None


def _nijivoice_descriptor(*, action: str, destination: str, model: str = ""):
    """Create the canonical outbound descriptor lazily."""

    try:
        from ...services.outbound_privacy_service import EgressDescriptor

        return EgressDescriptor(
            action=action,
            transport="httpx",
            destination=destination,
            provider="nijivoice",
            tool="tts.nijivoice",
            model=model,
        )
    except ImportError:  # pragma: no cover - stripped/legacy embedding
        from types import SimpleNamespace

        return SimpleNamespace(
            action=action,
            transport="httpx",
            destination=destination,
            provider="nijivoice",
            tool="tts.nijivoice",
            model=model,
        )


class NijivoiceEngine:
    """Nijivoice Text-to-Speech engine"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.nijivoice.com",
        config: Any | None = None,
        privacy_gateway: Any | None = None,
    ):
        """
        Initialize Nijivoice engine
        
        Args:
            api_key: API key for Nijivoice service
            base_url: Base URL for Nijivoice API
        """
        self.api_key = api_key or os.getenv("NIJIVOICE_API_KEY")
        self.base_url = base_url.rstrip('/')
        self.config = config
        self._privacy_gateway = privacy_gateway
        self.client = None
        self.voices = []
        self.current_voice = None

    def _gateway(self) -> OutboundPrivacyGateway:
        """Return a request-scoped gateway with the active privacy scope."""

        if self._privacy_gateway is not None:
            return self._privacy_gateway
        try:
            from ...services.turn_context import get_turn_context

            turn = get_turn_context()
        except Exception:
            turn = None
        scope = get_privacy_policy_context()
        return OutboundPrivacyGateway(
            self.config,
            user_id=str(getattr(turn, "user_id", "") or ""),
            session_id=str(getattr(turn, "session_id", "") or ""),
            session_context=scope.session_context,
            project_metadata=scope.project_metadata,
        )

    async def _execute_external(
        self,
        payload: Any,
        *,
        action: str,
        destination: str,
        sender,
    ) -> Any:
        """Run one Nijivoice request through the single outbound boundary."""

        gateway = self._gateway()
        execute = getattr(gateway, "execute", None)
        if not callable(execute):
            # Never fall back to a raw client or a pre-protected payload: a
            # mixed-version runtime without the transaction API must fail
            # closed rather than bypass review/masking.
            raise PrivacyError("outbound privacy gateway does not support execution")
        descriptor = _nijivoice_descriptor(
            action=action,
            destination=destination,
        )
        return await execute(
            payload,
            provider="nijivoice",
            descriptor=descriptor,
            sender=sender,
            base_url=self.base_url,
            source_kind=action,
        )
        
    async def initialize(self) -> bool:
        """Initialize the Nijivoice engine"""
        try:
            if not self.api_key:
                print("[Nijivoice] Error: API key not provided")
                return False
                
            # Create async HTTP client
            client_kwargs = {
                "headers": {
                    "accept": "application/json",
                    "x-api-key": self.api_key,
                },
                "timeout": 30.0,
                # Provider redirects are not part of the Nijivoice contract;
                # keeping them disabled prevents an unreviewed second hop.
                "follow_redirects": False,
            }
            try:
                self.client = httpx.AsyncClient(**client_kwargs)
            except TypeError as exc:
                # Small test/embedding clients may expose only headers and
                # timeout.  Do not hide unrelated constructor failures.
                if "follow_redirects" not in str(exc):
                    raise
                client_kwargs.pop("follow_redirects", None)
                self.client = httpx.AsyncClient(**client_kwargs)
            
            # Test connection and get available voices
            await self._get_voices()
            
            if self.voices:
                self.current_voice = self.voices[0]
                print(f"[Nijivoice] Initialized successfully with {len(self.voices)} voices")
                return True
            else:
                print("[Nijivoice] Warning: No voices available")
                return True
                
        except Exception as e:
            print(f"[Nijivoice] Initialization error: {e}")
            return False
    
    async def _get_voices(self) -> List[Dict[str, Any]]:
        """Get available voices from Nijivoice API"""
        try:
            endpoint = f"{self.base_url}/api/platform/v1/voice-actors"

            async def send(_payload):
                return await self.client.get(endpoint)

            response = await self._execute_external(
                {},
                action="tts.nijivoice.voice_actors",
                destination=endpoint,
                sender=send,
            )
            response.raise_for_status()
            
            data = response.json()
            # The API returns {"voiceActors": [...]}
            self.voices = data.get("voiceActors", [])
            
            print(f"[Nijivoice] Found {len(self.voices)} voices")
            return self.voices
            
        except Exception as e:
            print(f"[Nijivoice] Error getting voices: {e}")
            self.voices = []
            return []
    
    async def synthesize(self, text: str, **kwargs) -> Optional[bytes]:
        """
        Synthesize speech from text
        
        Args:
            text: Text to synthesize
            **kwargs: Additional parameters:
                - voice_id: Voice Actor ID to use
                - format: Output format (mp3, wav, etc.)
                
        Returns:
            Audio data as bytes or None on error
        """
        if not self.client:
            print("[Nijivoice] Error: Engine not initialized")
            return None
            
        try:
            # Prepare synthesis parameters
            voice_id = kwargs.get('voice_id')
            if not voice_id and self.current_voice:
                voice_id = self.current_voice.get('id')
                
            if not voice_id:
                print("[Nijivoice] Error: No voice selected")
                return None
            
            # Prepare request payload
            payload = {
                "script": text,
                "format": kwargs.get('format', 'mp3'),
                "speed": str(kwargs.get('speed', self.current_voice.get('recommendedVoiceSpeed', 1.0))),
                "emotionalLevel": str(kwargs.get('emotionalLevel', self.current_voice.get('recommendedEmotionalLevel', 0.1))),
                "soundDuration": str(kwargs.get('soundDuration', self.current_voice.get('recommendedSoundDuration', 0.1)))
            }
            
            # Make synthesis request using the voice actor ID
            endpoint = (
                f"{self.base_url}/api/platform/v1/voice-actors/"
                f"{voice_id}/generate-voice"
            )

            async def send(protected_payload):
                if not isinstance(protected_payload, Mapping):
                    raise RuntimeError("privacy protection returned no protected payload")
                # The gateway's final candidate is the only payload sent to
                # the provider.  Do not re-read the raw text from kwargs.
                return await self.client.post(endpoint, json=dict(protected_payload))

            response = await self._execute_external(
                payload,
                action="tts.nijivoice.synthesize",
                destination=endpoint,
                sender=send,
            )
            response.raise_for_status()
            
            # Debug: Print response content type and first bytes
            content_type = response.headers.get('content-type', '')
            print(f"[Nijivoice] Response content-type: {content_type}")
            
            # The API returns JSON with audio URL
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                print(f"[Nijivoice] JSON decode error: {e}")
                print(f"[Nijivoice] Response text: {response.text[:200]}...")
                return None
            
            # Extract audio URL from response
            generated_voice = data.get('generatedVoice', {})
            audio_url = generated_voice.get('audioFileUrl') or generated_voice.get('audioFileDownloadUrl')
            
            if not audio_url:
                print(f"[Nijivoice] No audio URL in response: {data}")
                return None
            
            # Download the audio file
            print(f"[Nijivoice] Downloading audio from: {audio_url}")
            async def download(protected_payload):
                # URL values are provider output, not user-authored text, but
                # the follow-up request still crosses the same no-redirect
                # egress boundary and is never allowed to follow a second hop.
                target = (
                    protected_payload.get("url", audio_url)
                    if isinstance(protected_payload, Mapping)
                    else audio_url
                )
                return await self.client.get(target)

            audio_response = await self._execute_external(
                {"url": audio_url},
                action="tts.nijivoice.audio_download",
                destination=str(audio_url),
                sender=download,
            )
            audio_response.raise_for_status()
            
            # Convert MP3 to WAV for compatibility with AudioPlayer
            try:
                mp3_data = audio_response.content
                print(f"[Nijivoice] Downloaded MP3 data: {len(mp3_data)} bytes")
                
                # Check if we actually got MP3 data
                if len(mp3_data) == 0:
                    print("[Nijivoice] Error: Empty audio data received")
                    return None
                
                # Try multiple methods to convert MP3 to WAV
                try:
                    # Method 1: Try with file-based approach
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_mp3:
                        tmp_mp3.write(mp3_data)
                        tmp_mp3_path = tmp_mp3.name
                    
                    try:
                        # Configure pydub to handle ffmpeg issues
                        from pydub.utils import which
                        AudioSegment.converter = which("ffmpeg")
                        
                        # Load MP3 from file
                        audio_segment = AudioSegment.from_mp3(tmp_mp3_path)
                        
                        # Convert to WAV format (16-bit PCM, mono, 24000Hz for compatibility)
                        audio_segment = audio_segment.set_frame_rate(24000).set_channels(1).set_sample_width(2)
                        
                        wav_buffer = io.BytesIO()
                        audio_segment.export(wav_buffer, format="wav")
                        wav_buffer.seek(0)
                        wav_data = wav_buffer.getvalue()
                        
                        print(f"[Nijivoice] Converted MP3 ({len(mp3_data)} bytes) to WAV ({len(wav_data)} bytes)")
                        return wav_data
                    finally:
                        # Clean up temp file
                        import os as os_module
                        try:
                            os_module.unlink(tmp_mp3_path)
                        except:
                            pass
                    
                except Exception as e:
                    print(f"[Nijivoice] Method 1 failed: {e}")
                    
                    # Method 2: Try direct ffmpeg conversion
                    import subprocess
                    import tempfile
                    
                    try:
                        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_mp3:
                            tmp_mp3.write(mp3_data)
                            tmp_mp3_path = tmp_mp3.name
                        
                        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_wav:
                            tmp_wav_path = tmp_wav.name
                        
                        # Use ffmpeg directly with LD_LIBRARY_PATH fix for WSL
                        import os as os_module
                        env = os_module.environ.copy()
                        # Remove conflicting library paths that might cause issues
                        if 'LD_LIBRARY_PATH' in env:
                            del env['LD_LIBRARY_PATH']
                        
                        cmd = [
                            'ffmpeg', '-y', '-loglevel', 'error', '-i', tmp_mp3_path,
                            '-acodec', 'pcm_s16le', '-ar', '24000', '-ac', '1',
                            tmp_wav_path
                        ]
                        
                        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
                        
                        if result.returncode == 0:
                            with open(tmp_wav_path, 'rb') as f:
                                wav_data = f.read()
                            print(f"[Nijivoice] Converted MP3 to WAV using ffmpeg: {len(wav_data)} bytes")
                            return wav_data
                        else:
                            print(f"[Nijivoice] ffmpeg failed: {result.stderr}")
                            
                    except Exception as e2:
                        print(f"[Nijivoice] Method 2 failed: {e2}")
                    finally:
                        # Clean up temp files
                        for path in [tmp_mp3_path, tmp_wav_path]:
                            try:
                                os_module.unlink(path)
                            except:
                                pass
                
                # If all conversion methods fail, return None
                print("[Nijivoice] All conversion methods failed")
                return None
                
            except Exception as e:
                print(f"[Nijivoice] Error in audio processing: {e}")
                import traceback
                traceback.print_exc()
                return None
            
        except httpx.HTTPStatusError as e:
            print(f"[Nijivoice] HTTP error {e.response.status_code}: {e.response.text}")
            return None
        except Exception as e:
            print(f"[Nijivoice] Synthesis error: {e}")
            return None
    
    def get_voices(self) -> List[Dict[str, Any]]:
        """Get list of available voices"""
        return self.voices
    
    def set_voice_by_id(self, voice_id: str) -> bool:
        """Set current voice by ID"""
        for voice in self.voices:
            if voice.get('id') == voice_id:
                self.current_voice = voice
                print(f"[Nijivoice] Voice set to: {voice.get('name', voice_id)}")
                return True
        print(f"[Nijivoice] Voice ID not found: {voice_id}")
        return False
    
    def set_voice_by_name(self, voice_name: str) -> bool:
        """Set current voice by name"""
        for voice in self.voices:
            if voice.get('name', '').lower() == voice_name.lower():
                self.current_voice = voice
                print(f"[Nijivoice] Voice set to: {voice.get('name')}")
                return True
        print(f"[Nijivoice] Voice name not found: {voice_name}")
        return False
    
    async def cleanup(self):
        """Cleanup resources"""
        if self.client:
            await self.client.aclose()
            self.client = None
    
    def __del__(self):
        """Cleanup on deletion"""
        # Note: Do not use asyncio.create_task() here as the event loop
        # may already be closed, causing ConnectionResetError on Windows.
        # Client cleanup should be done via explicit cleanup() call.
        pass
