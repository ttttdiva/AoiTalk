"""
VOICEVOX TTS engine implementation
"""
import asyncio
import subprocess
import time
import os
import socket
import psutil
from collections.abc import Mapping
from typing import Optional, Dict, Any
import requests
import aiohttp

from ...services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)

from ..engine_startup import (
    DEFAULT_ENGINE_STARTUP_TIMEOUT_SECONDS,
    wait_for_http_readiness,
)

try:
    from voicevox import Client as VoicevoxClient
except ImportError:
    try:
        from vvclient import Client as VoicevoxClient
    except ImportError:
        # VOICEVOX is an optional local/server-backed engine.  Keep the TTS
        # manager importable for Linux/Enterprise builds that intentionally
        # omit the audio extra; selecting this engine still fails explicitly.
        VoicevoxClient = None


class VoicevoxEngine:
    """VOICEVOX Text-to-Speech engine"""
    
    def __init__(
        self,
        engine_path: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 50021,
        startup_timeout_seconds: float = DEFAULT_ENGINE_STARTUP_TIMEOUT_SECONDS,
        config: Any | None = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ):
        """Initialize VOICEVOX engine
        
        Args:
            engine_path: Path to VOICEVOX engine executable
            host: Host address for VOICEVOX server
            port: Port number for VOICEVOX server
            startup_timeout_seconds: Absolute startup readiness deadline
        """
        self.engine_path = engine_path
        self.host = host
        self.port = port
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.base_url = f"http://{host}:{port}"
        self.config = config
        self._privacy_gateway = privacy_gateway
        self.process = None
        self.client = None
        self.session = None  # aiohttp session for connection pooling

    def _gateway(self) -> OutboundPrivacyGateway:
        """Return the active request-scoped privacy gateway."""

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

    def _descriptor(self, *, action: str, path: str) -> EgressDescriptor:
        return EgressDescriptor(
            action=action,
            transport="aiohttp",
            destination=f"{self.base_url}{path}",
            provider="voicevox",
            tool="tts.voicevox",
        )

    async def _execute_async(
        self,
        payload: Any,
        *,
        action: str,
        path: str,
        sender,
    ) -> Any:
        return await self._gateway().execute(
            payload,
            provider="voicevox",
            descriptor=self._descriptor(action=action, path=path),
            sender=sender,
            base_url=self.base_url,
            source_kind=action,
        )

    def _execute_sync(
        self,
        payload: Any,
        *,
        action: str,
        path: str,
        sender,
    ) -> Any:
        return self._gateway().execute_sync(
            payload,
            provider="voicevox",
            descriptor=self._descriptor(action=action, path=path),
            sender=sender,
            base_url=self.base_url,
            source_kind=action,
        )

    def _create_client(self):
        if VoicevoxClient is None:
            raise RuntimeError(
                "VOICEVOX requires the optional voicevox-client or vvclient package"
            )
        try:
            return VoicevoxClient(base_url=self.base_url)
        except TypeError:
            return VoicevoxClient(base_uri=self.base_url)

    async def _attach_session(self):
        if not self.client or not self.session:
            return

        if hasattr(self.client, '_session'):
            if self.client._session and not self.client._session.closed:
                await self.client._session.close()
            self.client._session = self.session
            return

        http_client = getattr(self.client, 'http', None)
        if http_client and hasattr(http_client, '_session'):
            if http_client._session and not http_client._session.closed:
                await http_client._session.close()
            http_client._session = self.session

    @staticmethod
    def _set_audio_query_value(audio_query: Any, attr_name: str, api_key: str, value: float):
        if hasattr(audio_query, attr_name):
            setattr(audio_query, attr_name, value)
            return

        data = getattr(audio_query, 'data', None)
        if isinstance(data, dict):
            data[api_key] = value
        
    def _is_port_in_use(self, port: int) -> bool:
        """Check if port is in use
        
        Args:
            port: Port number to check
            
        Returns:
            True if port is in use
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                # Set a short timeout to avoid blocking in WSL2
                sock.settimeout(0.1)
                result = sock.connect_ex(('127.0.0.1', port))
                return result == 0
        except Exception:
            return False
            
    def _kill_process_using_port(self, port: int) -> bool:
        """Kill process using specified port
        
        Args:
            port: Port number
            
        Returns:
            True if process was killed successfully
        """
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    if conn.pid:
                        print(f"[VOICEVOX] Killing process {conn.pid} using port {port}")
                        try:
                            process = psutil.Process(conn.pid)
                            process.terminate()
                            process.wait(timeout=3)
                            return True
                        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                            try:
                                process.kill()
                                return True
                            except psutil.NoSuchProcess:
                                return True
                        except Exception as e:
                            print(f"[VOICEVOX] Error killing process: {e}")
                            return False
            return True  # No process found using the port
        except Exception as e:
            print(f"[VOICEVOX] Error checking port usage: {e}")
            return False
        
    def start_engine(self) -> bool:
        """Start VOICEVOX engine process
        
        Returns:
            True if engine started successfully
        """
        if not self.engine_path:
            print("VOICEVOX engine path not specified")
            return False
            
        # Expand environment variables in path
        expanded_path = os.path.expandvars(self.engine_path)
        if not os.path.exists(expanded_path):
            print(f"VOICEVOX engine not found at: {self.engine_path}")
            print(f"Expanded path: {expanded_path}")
            return False
            
        # Update engine_path to expanded version
        self.engine_path = expanded_path
        print(f"Starting VOICEVOX engine from: {self.engine_path}")
        
        # Check if port is already in use (using socket method which works in WSL2)
        if self._is_port_in_use(self.port):
            print(f"[VOICEVOX] Port {self.port} is already in use")
            # Try simple approach: wait a bit and retry
            print(f"[VOICEVOX] Waiting 3 seconds for port to be released...")
            time.sleep(3)
            if self._is_port_in_use(self.port):
                print(f"[VOICEVOX] Port {self.port} is still in use. Proceeding anyway...")
                # Note: psutil.net_connections() blocks in WSL2, so we skip process killing
        
        # Start VOICEVOX engine
        # Note: Removed --use_gpu to avoid potential GPU initialization blocking in WSL2
        cmd = [self.engine_path, "--host", self.host, "--port", str(self.port)]
        
        try:
            cwd = os.path.dirname(self.engine_path)
            
            # Start process with DEVNULL to avoid blocking in WSL2
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                cwd=cwd
            )
            
            print(f"Process ID: {self.process.pid}")

            print("Waiting for VOICEVOX engine to start...")
            deadline = time.monotonic() + self.startup_timeout_seconds
            started = wait_for_http_readiness(
                self.base_url,
                deadline=deadline,
                process_alive=lambda: self.process is not None and self.process.poll() is None,
            )
            if started:
                try:
                    response = self._execute_sync(
                        {},
                        action="tts.voicevox.version",
                        path="/version",
                        sender=lambda _payload: requests.get(
                            f"{self.base_url}/version",
                            timeout=2,
                            allow_redirects=False,
                        ),
                    )
                    version_info = response.text.strip().replace('"', '')
                except requests.exceptions.RequestException:
                    version_info = "unknown"
                elapsed_time = int(time.monotonic() - (deadline - self.startup_timeout_seconds))
                print(f"VOICEVOX engine started successfully after {elapsed_time} seconds")
                print(f"Version: {version_info}")
                return True

            if self.process.poll() is not None:
                print(f"VOICEVOX engine failed to start (exit code: {self.process.returncode})")
            else:
                print(
                    "VOICEVOX engine startup timed out after "
                    f"{int(self.startup_timeout_seconds)} seconds"
                )
            self.stop_engine()
            return False
            
        except Exception as e:
            print(f"Failed to start VOICEVOX engine: {e}")
            self.stop_engine()
            return False
            
    def stop_engine(self):
        """Stop VOICEVOX engine process"""
        if self.process:
            print("Stopping VOICEVOX engine...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            
    async def initialize(self) -> bool:
        """Initialize VOICEVOX client
        
        Returns:
            True if initialization successful
        """
        try:
            # Create custom session with connection pooling optimized for Windows
            connector = aiohttp.TCPConnector(
                limit=20,  # Reduced total connection pool limit for Windows
                limit_per_host=10,  # Reduced per-host connection limit
                ttl_dns_cache=300,  # DNS cache timeout
                force_close=False,  # Allow connection reuse
                enable_cleanup_closed=True  # Clean up closed connections
            )
            timeout = aiohttp.ClientTimeout(total=30, connect=5)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={'Connection': 'keep-alive'}  # Ensure connection reuse
            )
            
            # Initialize client with custom session
            self.client = self._create_client()
            await self._attach_session()
            
            return True
        except Exception as e:
            print(f"Failed to initialize VOICEVOX client: {e}")
            return False
            
    async def synthesize(self, 
                        text: str, 
                        speaker_id: int = 3,
                        speed: float = 1.0,
                        pitch: float = 0.0,
                        intonation: float = 1.0,
                        volume: float = 1.0) -> Optional[bytes]:
        """Synthesize speech from text
        
        Args:
            text: Text to synthesize
            speaker_id: VOICEVOX speaker ID
            speed: Speech speed (0.5-2.0)
            pitch: Voice pitch (-0.15-0.15)
            intonation: Intonation scale (0.0-2.0)
            volume: Volume scale (0.0-2.0)
            
        Returns:
            WAV audio data as bytes, or None if failed
        """
        if not self.client:
            print("[VOICEVOX] Client not initialized")
            return None
            
        # Check if engine is still running.  Reinitialization/retry is left to
        # an explicit caller so every provider request has one auditable
        # gateway transaction.
        if self.process and self.process.poll() is not None:
            print(f"[VOICEVOX] Engine process died (exit code: {self.process.returncode})")
            return None
            
        # No hidden retries: retrying here could bypass a fresh review
        # decision and make provider request counts opaque.
        max_retries = 1
        
        for attempt in range(max_retries):
            try:
                # Create audio query
                def send_audio_query(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "VOICEVOX audio-query payload is malformed"
                        )
                    outbound_text = protected_payload.get("text")
                    if not isinstance(outbound_text, str):
                        raise PrivacyError(
                            "VOICEVOX audio-query text is unavailable"
                        )
                    try:
                        outbound_speaker = int(
                            protected_payload.get("speaker", speaker_id)
                        )
                    except (TypeError, ValueError) as exc:
                        raise PrivacyError(
                            "VOICEVOX audio-query speaker is invalid"
                        ) from exc
                    return self.client.create_audio_query(
                        outbound_text,
                        speaker=outbound_speaker,
                    )

                audio_query = await self._execute_async(
                    {"text": text, "speaker": speaker_id},
                    action="tts.voicevox.audio_query",
                    path="/audio_query",
                    sender=send_audio_query,
                )
                
                # Apply voice parameters
                self._set_audio_query_value(audio_query, 'speed_scale', 'speedScale', speed)
                self._set_audio_query_value(audio_query, 'pitch_scale', 'pitchScale', pitch)
                self._set_audio_query_value(audio_query, 'intonation_scale', 'intonationScale', intonation)
                self._set_audio_query_value(audio_query, 'volume_scale', 'volumeScale', volume)
                
                # Synthesize audio through the gateway as a separate request.
                def send_synthesis(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "VOICEVOX synthesis payload is malformed"
                        )
                    outbound_query = protected_payload.get("audio_query")
                    if outbound_query is not audio_query:
                        # ``audio_query`` is the object returned by the first
                        # provider transaction.  A reviewer may redact scalar
                        # fields in a mapping copy, but the SDK's synthesis
                        # method requires that approved object; never fall
                        # back to the pre-review value when it is omitted.
                        if outbound_query is None:
                            raise PrivacyError(
                                "VOICEVOX synthesis audio query is unavailable"
                            )
                    try:
                        outbound_speaker = int(
                            protected_payload.get("speaker", speaker_id)
                        )
                    except (TypeError, ValueError) as exc:
                        raise PrivacyError(
                            "VOICEVOX synthesis speaker is invalid"
                        ) from exc
                    # The SDK method is bound to the query object returned by
                    # ``create_audio_query``.  Preserve this exact approved
                    # object rather than reaching back to raw request state.
                    query_to_send = outbound_query
                    synth = getattr(query_to_send, "synthesis", None)
                    if not callable(synth):
                        raise PrivacyError(
                            "VOICEVOX synthesis audio query is not sendable"
                        )
                    return synth(speaker=outbound_speaker)

                audio_data = await self._execute_async(
                    {"speaker": speaker_id, "audio_query": audio_query},
                    action="tts.voicevox.synthesis",
                    path="/synthesis",
                    sender=send_synthesis,
                )
                return audio_data
                
            except Exception as e:
                print(f"[VOICEVOX] Synthesis error: {type(e).__name__}: {e}")
                return None
                    
        return None
            
    async def get_speakers(self) -> Optional[list]:
        """Get available speakers
        
        Returns:
            List of available speakers
        """
        if not self.client:
            return None

        try:
            if hasattr(self.client, 'fetch_speakers'):
                return await self._execute_async(
                    {},
                    action="tts.voicevox.speakers",
                    path="/speakers",
                    sender=lambda _payload: self.client.fetch_speakers(),
                )

            session = self.session
            owns_session = False
            if not session or session.closed:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=5))
                owns_session = True

            try:
                async def send_speakers(_payload: Any) -> Any:
                    async with session.get(
                        f"{self.base_url}/speakers",
                        allow_redirects=False,
                    ) as response:
                        response.raise_for_status()
                        return await response.json()

                return await self._execute_async(
                    {},
                    action="tts.voicevox.speakers",
                    path="/speakers",
                    sender=send_speakers,
                )
            finally:
                if owns_session:
                    await session.close()
        except Exception as e:
            print(f"Failed to fetch speakers: {e}")
            return None
            
    async def cleanup(self):
        """Cleanup resources"""
        # Close client properly
        if self.client:
            try:
                await self.client.close()
            except:
                pass
            self.client = None
            
        # Close aiohttp session
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
        
        # Stop engine
        self.stop_engine()
        
    def __del__(self):
        """Cleanup on deletion"""
        # Note: Do not use asyncio.create_task() here as the event loop
        # may already be closed, causing ConnectionResetError on Windows.
        # Session cleanup should be done via explicit cleanup() call.
        self.stop_engine()
        
    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.cleanup()
