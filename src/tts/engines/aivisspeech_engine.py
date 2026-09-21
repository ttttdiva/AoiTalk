"""
AivisSpeech TTS engine implementation
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
import httpx

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


class AivisSpeechEngine:
    """AivisSpeech Text-to-Speech engine"""
    
    def __init__(
        self,
        engine_path: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 10101,
        use_gpu: bool = False,
        startup_timeout_seconds: float = DEFAULT_ENGINE_STARTUP_TIMEOUT_SECONDS,
        config: Any | None = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ):
        """Initialize AivisSpeech engine
        
        Args:
            engine_path: Path to AivisSpeech engine executable
            host: Host address for AivisSpeech server
            port: Port number for AivisSpeech server (default: 10101)
            use_gpu: Whether to use GPU acceleration on Windows
            startup_timeout_seconds: Absolute startup readiness deadline
        """
        self.engine_path = engine_path
        self.host = host
        self.port = port
        self.use_gpu = use_gpu
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.base_url = f"http://{host}:{port}"
        self.config = config
        self._privacy_gateway = privacy_gateway
        self.process = None
        self.client = None  # Persistent httpx client

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
            transport="httpx" if self.client is not None else "requests",
            destination=f"{self.base_url}{path}",
            provider="aivisspeech",
            tool="tts.aivisspeech",
        )

    async def _execute_async(
        self,
        payload: Any,
        *,
        action: str,
        path: str,
        sender,
    ) -> Any:
        gateway = self._gateway()
        return await gateway.execute(
            payload,
            provider="aivisspeech",
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
        gateway = self._gateway()
        return gateway.execute_sync(
            payload,
            provider="aivisspeech",
            descriptor=self._descriptor(action=action, path=path),
            sender=sender,
            base_url=self.base_url,
            source_kind=action,
        )
        
    def _is_port_in_use(self, port: int) -> bool:
        """Check if port is in use
        
        Args:
            port: Port number to check
            
        Returns:
            True if port is in use
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
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
                        print(f"[AivisSpeech] Killing process {conn.pid} using port {port}")
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
                            print(f"[AivisSpeech] Error killing process: {e}")
                            return False
            return True  # No process found using the port
        except Exception as e:
            print(f"[AivisSpeech] Error checking port usage: {e}")
            return False
        
    def start_engine(self) -> bool:
        """Start AivisSpeech engine process
        
        Returns:
            True if engine started successfully
        """
        if not self.engine_path:
            print("AivisSpeech engine path not specified")
            return False
            
        # Expand environment variables in path
        expanded_path = os.path.expandvars(self.engine_path)
        
        # Check if the path points to the GUI editor and fix it
        if expanded_path.endswith('AivisSpeech.exe'):
            # Convert GUI path to engine path
            engine_dir = os.path.dirname(expanded_path)
            engine_path = os.path.join(engine_dir, 'AivisSpeech-Engine', 'run.exe')
            if os.path.exists(engine_path):
                print(f"[AivisSpeech] Detected GUI path, switching to engine: {engine_path}")
                expanded_path = engine_path
            else:
                # Try alternative engine path structure
                alt_engine_path = os.path.join(engine_dir, 'engine', 'run.exe')
                if os.path.exists(alt_engine_path):
                    print(f"[AivisSpeech] Found alternative engine path: {alt_engine_path}")
                    expanded_path = alt_engine_path
                else:
                    print(f"[AivisSpeech] Engine not found at expected locations:")
                    print(f"  - {engine_path}")
                    print(f"  - {alt_engine_path}")
                    return False
        
        if not os.path.exists(expanded_path):
            print(f"AivisSpeech engine not found at: {self.engine_path}")
            print(f"Expanded path: {expanded_path}")
            
            # Additional Windows debugging information
            if os.name == 'nt':
                print("\n[AivisSpeech] Windows path debugging:")
                parent_dir = os.path.dirname(expanded_path)
                if os.path.exists(parent_dir):
                    print(f"Parent directory exists: {parent_dir}")
                    try:
                        files_in_dir = os.listdir(parent_dir)
                        exe_files = [f for f in files_in_dir if f.endswith('.exe')]
                        print(f"Executable files in directory: {exe_files}")
                    except Exception as e:
                        print(f"Error listing directory: {e}")
                else:
                    print(f"Parent directory does not exist: {parent_dir}")
            
            return False
            
        # Windows executable validation
        if os.name == 'nt':
            print(f"[AivisSpeech] Validating Windows executable: {expanded_path}")
            # Check if it's actually an executable file
            if not expanded_path.endswith('.exe'):
                print(f"[AivisSpeech] Warning: File does not have .exe extension")
            
            # Check file size (basic sanity check)
            try:
                file_size = os.path.getsize(expanded_path)
                if file_size < 1000:  # Very small file, likely not a real executable
                    print(f"[AivisSpeech] Warning: Executable file is very small ({file_size} bytes)")
                else:
                    print(f"[AivisSpeech] Executable file size: {file_size} bytes")
            except Exception as e:
                print(f"[AivisSpeech] Error checking file size: {e}")
            
            # Test if the executable can be run with --help flag
            try:
                print(f"[AivisSpeech] Testing executable with --help flag...")
                test_result = subprocess.run(
                    [expanded_path, "--help"], 
                    capture_output=True, 
                    text=True, 
                    timeout=10,
                    cwd=os.path.dirname(expanded_path)
                )
                if test_result.returncode == 0:
                    print(f"[AivisSpeech] Executable test successful")
                else:
                    print(f"[AivisSpeech] Executable test failed (return code: {test_result.returncode})")
                    if test_result.stderr:
                        print(f"[AivisSpeech] Test error output: {test_result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"[AivisSpeech] Executable test timed out (may be normal)")
            except Exception as e:
                print(f"[AivisSpeech] Executable test error: {e}")
                # Check for common Windows dependency issues
                error_str = str(e).lower()
                if "dll" in error_str or "library" in error_str:
                    print(f"[AivisSpeech] Possible DLL dependency issue detected")
                    print(f"[AivisSpeech] Please ensure Microsoft Visual C++ Redistributable is installed")
            
        # Update engine_path to expanded version
        self.engine_path = expanded_path
        print(f"Starting AivisSpeech engine from: {self.engine_path}")
        
        # Log the final command that will be executed
        print(f"[AivisSpeech] Working directory: {os.path.dirname(self.engine_path)}")
        
        # Check if port is already in use and kill existing process
        if self._is_port_in_use(self.port):
            print(f"[AivisSpeech] Port {self.port} is already in use, attempting to kill existing process...")
            if self._kill_process_using_port(self.port):
                print(f"[AivisSpeech] Successfully killed process using port {self.port}")
                time.sleep(2)  # Wait for port to be released
            else:
                print(f"[AivisSpeech] Failed to kill process using port {self.port}")
                return False
        
        # Start AivisSpeech engine in headless mode
        cmd = [self.engine_path, "--host", self.host, "--port", str(self.port)]
        
        # IMPORTANT: Load all models at startup to avoid timeout issues on Windows
        cmd.append("--load_all_models")
        print("[AivisSpeech] Loading all models at startup to prevent timeout issues")
        
        # Windows optimization: Skip unsupported dictionary parameters
        if os.name == 'nt':  # Windows only
            print("[AivisSpeech] Windows optimization: Using basic startup parameters")
        
        # Add optional parameters for better performance
        if os.name == 'nt':  # Windows
            # Check GPU availability from config
            # Don't use GPU by default as it may cause silent crashes on unsupported systems
            use_gpu = self.use_gpu if hasattr(self, 'use_gpu') else False
            if use_gpu:
                print("[AivisSpeech] GPU mode enabled (config: use_gpu=true)")
                cmd.append("--use_gpu")
            else:
                print("[AivisSpeech] CPU mode (set use_gpu=true in config to enable GPU)")
        
        # Log encoding for Windows console
        cmd.append("--output_log_utf8")
        
        # Log the final command for debugging
        print(f"[AivisSpeech] Final command: {' '.join(cmd)}")
        
        try:
            # Windows-specific process creation
            if os.name == 'nt':
                # Use simpler process creation to avoid popup errors
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE  # Hide console window
                
                # Prepare environment with proper encoding
                env = os.environ.copy()
                # Ensure proper locale for Windows
                env['PYTHONIOENCODING'] = 'utf-8'
                
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    cwd=os.path.dirname(self.engine_path),
                    env=env,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    cwd=os.path.dirname(self.engine_path),
                    env=os.environ.copy(),
                )
            
            print(f"Process ID: {self.process.pid}")
            
            # Immediately check if process crashed
            initial_check = self.process.poll()
            if initial_check is not None:
                print(f"[AivisSpeech] Process died immediately (exit code: {initial_check})")
                if os.name == 'nt':
                    print("[AivisSpeech] This typically indicates a Windows compatibility issue")
                    print("[AivisSpeech] Common causes:")
                    print("  - Missing Microsoft Visual C++ Redistributable")
                    print("  - Incompatible architecture (x86 vs x64)")
                    print("  - Antivirus software blocking execution")
                    print("  - Corrupted installation")
                return False
            
            # Wait for engine to start
            deadline = time.monotonic() + self.startup_timeout_seconds
            startup_started_at = time.monotonic()

            print("Waiting for AivisSpeech engine to start...")
            if os.name == 'nt':
                print("[AivisSpeech] Note: Dictionary optimizations enabled")
                print("[AivisSpeech] Windows: This may take longer on first run")

            started = wait_for_http_readiness(
                self.base_url,
                deadline=deadline,
                process_alive=lambda: self.process is not None and self.process.poll() is None,
            )
            if started:
                try:
                    response = self._execute_sync(
                        {},
                        action="tts.aivisspeech.version",
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
                elapsed_time = int(time.monotonic() - startup_started_at)
                print(f"AivisSpeech engine started successfully after {elapsed_time} seconds")
                print(f"Version: {version_info}")
                return True

            if self.process.poll() is not None:
                print(f"AivisSpeech engine failed to start (exit code: {self.process.returncode})")
            else:
                print(
                    "AivisSpeech engine startup timed out after "
                    f"{int(self.startup_timeout_seconds)} seconds"
                )
            self.stop_engine()
            return False
            
        except Exception as e:
            print(f"Failed to start AivisSpeech engine: {e}")
            self.stop_engine()
            return False
            
    def stop_engine(self):
        """Stop AivisSpeech engine process"""
        if self.process:
            print("Stopping AivisSpeech engine...")
            
            # Windows-specific termination
            if os.name == 'nt':
                try:
                    # Send CTRL_BREAK_EVENT on Windows for graceful shutdown
                    import signal
                    os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # If graceful shutdown fails, terminate
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            # Force kill as last resort
                            self.process.kill()
                except Exception as e:
                    print(f"[AivisSpeech] Error stopping engine: {e}")
                    try:
                        self.process.kill()
                    except:
                        pass
            else:
                # Unix-like systems
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            
            self.process = None
            
    async def initialize(self) -> bool:
        """Initialize AivisSpeech client
        
        Returns:
            True if initialization successful
        """
        try:
            # Create persistent client with optimized settings for Windows
            limits = httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0
            )
            timeout = httpx.Timeout(
                connect=5.0,
                read=60.0,
                write=30.0,
                pool=10.0
            )
            self.client = httpx.AsyncClient(
                base_url=self.base_url,
                limits=limits,
                timeout=timeout,
                headers={'Connection': 'keep-alive'},
                follow_redirects=False,
            )
            
            # Test connection to AivisSpeech API
            response = await self._execute_async(
                {},
                action="tts.aivisspeech.version",
                path="/version",
                sender=lambda _payload: self.client.get(
                    "/version",
                    follow_redirects=False,
                ),
            )
            if response.status_code == 200:
                print(f"[AivisSpeech] Connected to engine (version: {response.text.strip()})")
                return True
            else:
                print(f"[AivisSpeech] Failed to connect (status: {response.status_code})")
                await self.cleanup()
                return False
        except Exception as e:
            print(f"Failed to initialize AivisSpeech client: {e}")
            await self.cleanup()
            return False
            
    async def synthesize(self, 
                        text: str, 
                        speaker_id: int = 0,
                        speed: float = 1.0,
                        pitch: float = 0.0,
                        intonation: float = 1.0,
                        volume: float = 1.0) -> Optional[bytes]:
        """Synthesize speech from text
        
        Args:
            text: Text to synthesize
            speaker_id: AivisSpeech speaker ID
            speed: Speech speed (0.5-2.0)
            pitch: Voice pitch (-0.15-0.15)
            intonation: Intonation scale (0.0-2.0)
            volume: Volume scale (0.0-2.0)
            
        Returns:
            WAV audio data as bytes, or None if failed
        """
        # Windows-specific debug: immediate print before any async operations
        print(f"[AivisSpeech] synthesize() called - chars={len(str(text or ''))}, speaker_id: {speaker_id}")
        
        # On Windows, use synchronous method to avoid async issues
        if os.name == 'nt':
            print("[AivisSpeech] Windows detected - using synchronous synthesis")
            # Use run_in_executor to avoid blocking
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, 
                self.synthesize_sync, 
                text, speaker_id, speed, pitch, intonation, volume
            )
        
        if not self.client:
            print("[AivisSpeech] Client not initialized")
            return None
            
        # Check if engine is still running.  Do not silently restart/retry a
        # provider request; callers can explicitly reinitialize the engine.
        if self.process and self.process.poll() is not None:
            print(f"[AivisSpeech] Engine process died (exit code: {self.process.returncode})")
            return None
        
        # A single gateway transaction is intentional.  Hidden retries could
        # otherwise create a second provider request outside review/audit.
        max_retries = 1
        
        for attempt in range(max_retries):
            try:
                # Create audio query
                print(f"[AivisSpeech] Creating audio query - chars={len(str(text or ''))}, speaker_id: {speaker_id}")
                
                # Add timeout for Windows
                query_start = asyncio.get_event_loop().time()

                def send_audio_query(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "AivisSpeech audio-query payload is malformed"
                        )
                    return self.client.post(
                        "/audio_query",
                        params={
                            "text": str(protected_payload.get("text") or ""),
                            "speaker": int(protected_payload.get("speaker", speaker_id)),
                        },
                        follow_redirects=False,
                    )

                try:
                    query_response = await asyncio.wait_for(
                        self._execute_async(
                            {"text": text, "speaker": speaker_id},
                            action="tts.aivisspeech.audio_query",
                            path="/audio_query",
                            sender=send_audio_query,
                        ),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    print(f"[AivisSpeech] Audio query timed out after {asyncio.get_event_loop().time() - query_start:.1f}s")
                    raise Exception("Audio query timeout")
                
                if query_response.status_code != 200:
                    print(f"[AivisSpeech] Audio query failed: {query_response.status_code}")
                    try:
                        error_detail = query_response.text
                        if error_detail:
                            print(f"[AivisSpeech] Audio query error detail: {error_detail}")
                    except:
                        pass
                    if query_response.status_code >= 500:  # Server error, worth retrying
                        raise Exception(f"Server error: {query_response.status_code}")
                    return None
                
                print(f"[AivisSpeech] Audio query successful")
                
                audio_query = query_response.json()
                
                # Apply voice parameters
                audio_query["speedScale"] = speed
                audio_query["pitchScale"] = pitch
                audio_query["intonationScale"] = intonation
                audio_query["volumeScale"] = volume
                
                # Synthesize audio through the same gateway.  The provider
                # receives only the final audio-query candidate approved by
                # the transaction above.
                synth_start = asyncio.get_event_loop().time()

                def send_synthesis(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "AivisSpeech synthesis payload is malformed"
                        )
                    outbound_query = protected_payload.get("audio_query")
                    if not isinstance(outbound_query, Mapping):
                        raise PrivacyError(
                            "AivisSpeech synthesis audio query is unavailable"
                        )
                    return self.client.post(
                        "/synthesis",
                        params={
                            "speaker": int(protected_payload.get("speaker", speaker_id))
                        },
                        json=dict(outbound_query),
                        headers={"Content-Type": "application/json"},
                        follow_redirects=False,
                    )

                try:
                    synthesis_response = await asyncio.wait_for(
                        self._execute_async(
                            {"speaker": speaker_id, "audio_query": audio_query},
                            action="tts.aivisspeech.synthesis",
                            path="/synthesis",
                            sender=send_synthesis,
                        ),
                        timeout=20.0
                    )
                except asyncio.TimeoutError:
                    print(f"[AivisSpeech] Synthesis timed out after {asyncio.get_event_loop().time() - synth_start:.1f}s")
                    raise Exception("Synthesis timeout")
                
                if synthesis_response.status_code != 200:
                    print(f"[AivisSpeech] Synthesis failed: {synthesis_response.status_code}")
                    # Try to get error details
                    try:
                        error_detail = synthesis_response.text
                        if error_detail:
                            print(f"[AivisSpeech] Error detail: {error_detail}")
                    except:
                        pass
                    if synthesis_response.status_code >= 500:  # Server error, worth retrying
                        raise Exception(f"Server error: {synthesis_response.status_code}")
                    return None
                
                # Validate audio data
                audio_data = synthesis_response.content
                if not audio_data or len(audio_data) < 100:  # WAV header is at least 44 bytes
                    print(f"[AivisSpeech] Invalid audio data received (size: {len(audio_data) if audio_data else 0})")
                    return None
                    
                # Always log successful synthesis for debugging
                print(f"[AivisSpeech] Synthesis successful - size: {len(audio_data)} bytes")
                    
                return audio_data
                
            except Exception as e:
                print(f"[AivisSpeech] Synthesis error: {type(e).__name__}: {e}")
                return None
                    
        return None
    
    def synthesize_sync(self, 
                       text: str, 
                       speaker_id: int = 0,
                       speed: float = 1.0,
                       pitch: float = 0.0,
                       intonation: float = 1.0,
                       volume: float = 1.0) -> Optional[bytes]:
        """Synchronous synthesis method for Windows compatibility
        
        This method uses requests instead of httpx to avoid async issues on Windows.
        """
        print(f"[AivisSpeech] synthesize_sync() called - chars={len(str(text or ''))}, speaker_id: {speaker_id}")
        
        if self.process and self.process.poll() is not None:
            print(f"[AivisSpeech] Engine process died (exit code: {self.process.returncode})")
            return None
        
        # One explicit transaction; retrying here would bypass a fresh review
        # decision and make provider request counts opaque.
        max_retries = 1
        
        for attempt in range(max_retries):
            try:
                # Create audio query
                print(f"[AivisSpeech] Creating audio query - chars={len(str(text or ''))}, speaker_id: {speaker_id}")
                # Use longer timeout on Windows (30s for audio query)
                timeout = 30 if os.name == 'nt' else 10

                def send_audio_query(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "AivisSpeech audio-query payload is malformed"
                        )
                    return requests.post(
                        f"{self.base_url}/audio_query",
                        params={
                            "text": str(protected_payload.get("text") or ""),
                            "speaker": int(protected_payload.get("speaker", speaker_id)),
                        },
                        timeout=timeout,
                        allow_redirects=False,
                    )

                query_response = self._execute_sync(
                    {"text": text, "speaker": speaker_id},
                    action="tts.aivisspeech.audio_query",
                    path="/audio_query",
                    sender=send_audio_query,
                )
                
                if query_response.status_code != 200:
                    print(f"[AivisSpeech] Audio query failed: {query_response.status_code}")
                    if query_response.status_code >= 500:
                        raise Exception(f"Server error: {query_response.status_code}")
                    return None
                
                print(f"[AivisSpeech] Audio query successful")
                audio_query = query_response.json()
                
                # Apply voice parameters
                audio_query["speedScale"] = speed
                audio_query["pitchScale"] = pitch
                audio_query["intonationScale"] = intonation
                audio_query["volumeScale"] = volume
                
                # Synthesize audio
                print(f"[AivisSpeech] Synthesizing audio...")
                # Use longer timeout on Windows (60s for synthesis)
                timeout = 60 if os.name == 'nt' else 20

                def send_synthesis(protected_payload: Any):
                    if not isinstance(protected_payload, Mapping):
                        raise PrivacyError(
                            "AivisSpeech synthesis payload is malformed"
                        )
                    outbound_query = protected_payload.get("audio_query")
                    if not isinstance(outbound_query, Mapping):
                        raise PrivacyError(
                            "AivisSpeech synthesis audio query is unavailable"
                        )
                    return requests.post(
                        f"{self.base_url}/synthesis",
                        params={
                            "speaker": int(protected_payload.get("speaker", speaker_id))
                        },
                        json=dict(outbound_query),
                        headers={"Content-Type": "application/json"},
                        timeout=timeout,
                        allow_redirects=False,
                    )

                synthesis_response = self._execute_sync(
                    {"speaker": speaker_id, "audio_query": audio_query},
                    action="tts.aivisspeech.synthesis",
                    path="/synthesis",
                    sender=send_synthesis,
                )
                
                if synthesis_response.status_code != 200:
                    print(f"[AivisSpeech] Synthesis failed: {synthesis_response.status_code}")
                    if synthesis_response.status_code >= 500:
                        raise Exception(f"Server error: {synthesis_response.status_code}")
                    return None
                
                audio_data = synthesis_response.content
                if not audio_data or len(audio_data) < 100:
                    print(f"[AivisSpeech] Invalid audio data received (size: {len(audio_data) if audio_data else 0})")
                    return None
                    
                print(f"[AivisSpeech] Synthesis successful - size: {len(audio_data)} bytes")
                return audio_data
                
            except Exception as e:
                print(f"[AivisSpeech] Synthesis error: {type(e).__name__}: {e}")
                return None
                    
        return None
            
    async def get_speakers(self) -> Optional[list]:
        """Get available speakers
        
        Returns:
            List of available speakers
        """
        if not self.client:
            print("[AivisSpeech] Client not initialized")
            return None
            
        try:
            response = await self._execute_async(
                {},
                action="tts.aivisspeech.speakers",
                path="/speakers",
                sender=lambda _payload: self.client.get(
                    "/speakers",
                    follow_redirects=False,
                ),
            )
            if response.status_code == 200:
                speakers = response.json()
                print(f"[AivisSpeech] Available speakers: {speakers}")
                return speakers
            else:
                print(f"[AivisSpeech] Failed to fetch speakers: {response.status_code}")
                return None
        except Exception as e:
            print(f"Failed to fetch speakers: {e}")
            return None
            
    async def cleanup(self):
        """Cleanup resources"""
        # Close httpx client
        if self.client:
            try:
                await self.client.aclose()
            except:
                pass
            self.client = None
        
        # Stop engine
        self.stop_engine()
        
    def __del__(self):
        """Cleanup on deletion"""
        # Note: Do not use asyncio.create_task() here as the event loop
        # may already be closed, causing ConnectionResetError on Windows.
        # Client cleanup should be done via explicit cleanup() call.
        self.stop_engine()
        
    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.cleanup()
