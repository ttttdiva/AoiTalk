"""
AivisSpeech TTS engine implementation
"""
import asyncio
import subprocess
import time
import os
import socket
import psutil
from typing import Optional, Dict, Any
import requests
import httpx


class AivisSpeechEngine:
    """AivisSpeech Text-to-Speech engine"""
    
    def __init__(self, engine_path: Optional[str] = None, host: str = "127.0.0.1", port: int = 10101, use_gpu: bool = False):
        """Initialize AivisSpeech engine
        
        Args:
            engine_path: Path to AivisSpeech engine executable
            host: Host address for AivisSpeech server
            port: Port number for AivisSpeech server (default: 10101)
            use_gpu: Whether to use GPU acceleration on Windows
        """
        self.engine_path = engine_path
        self.host = host
        self.port = port
        self.use_gpu = use_gpu
        self.base_url = f"http://{host}:{port}"
        self.process = None
        self.client = None  # Persistent httpx client
        
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
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,  # Separate stderr to capture errors better
                    stdin=subprocess.PIPE,
                    shell=False,
                    cwd=os.path.dirname(self.engine_path),
                    env=env,
                    universal_newlines=True,
                    bufsize=1,
                    encoding='utf-8',
                    errors='replace',  # Replace invalid characters instead of ignoring
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW  # Use CREATE_NO_WINDOW instead
                )
            else:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    shell=False,
                    cwd=os.path.dirname(self.engine_path),
                    env=os.environ.copy(),
                    universal_newlines=True,
                    bufsize=1,
                    encoding='utf-8',
                    errors='ignore'
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
            start_time = time.time()
            
            print("Waiting for AivisSpeech engine to start...")
            if os.name == 'nt':
                print("[AivisSpeech] Note: Dictionary optimizations enabled")
                print("[AivisSpeech] Windows: This may take longer on first run")
            
            while True:
                # Check if process died
                if self.process.poll() is not None:
                    # Read remaining output and error
                    remaining_output = ""
                    remaining_error = ""
                    try:
                        remaining_output = self.process.stdout.read() if self.process.stdout else ""
                        remaining_error = self.process.stderr.read() if self.process.stderr else ""
                    except Exception:
                        pass
                    
                    print(f"AivisSpeech engine failed to start (exit code: {self.process.returncode})")
                    if remaining_output:
                        print(f"Output: {remaining_output}")
                    if remaining_error:
                        print(f"Error: {remaining_error}")
                    
                    # Provide helpful error guidance for Windows
                    if os.name == 'nt' and self.process.returncode != 0:
                        print("[AivisSpeech] Windows troubleshooting suggestions:")
                        print("1. Check if Microsoft Visual C++ Redistributable is installed")
                        print("2. Verify the engine path points to the correct run.exe")
                        print("3. Try running the engine manually first to check for errors")
                        print("4. Check Windows Defender/antivirus blocking the executable")
                    
                    return False
                
                # Read process output
                try:
                    line = self.process.stdout.readline()
                    if line and line.strip():
                        print(f"AivisSpeech: {line.strip()}")
                    
                    # Check stderr for error messages (Windows compatible)
                    if os.name == 'nt':
                        try:
                            # Check if stderr has data available (Windows compatible)
                            if self.process.stderr and hasattr(self.process.stderr, 'peek'):
                                # Try peek first to avoid blocking
                                peek_data = self.process.stderr.peek(1)
                                if peek_data:
                                    error_line = self.process.stderr.readline()
                                    if error_line and error_line.strip():
                                        print(f"AivisSpeech Error: {error_line.strip()}")
                        except Exception:
                            # Ignore stderr reading errors to prevent blocking
                            pass
                except Exception:
                    pass
                
                # Test HTTP connection
                try:
                    response = requests.get(f"{self.base_url}/version", timeout=2)
                    if response.status_code == 200:
                        version_info = response.text.strip().replace('"', '')
                        elapsed_time = int(time.time() - start_time)
                        print(f"AivisSpeech engine started successfully after {elapsed_time} seconds")
                        print(f"Version: {version_info}")
                        return True
                except requests.exceptions.RequestException:
                    pass
                    
                time.sleep(1)
            
        except Exception as e:
            print(f"Failed to start AivisSpeech engine: {e}")
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
                headers={'Connection': 'keep-alive'}
            )
            
            # Test connection to AivisSpeech API
            response = await self.client.get("/version")
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
        print(f"[AivisSpeech] synthesize() called - text: '{text}', speaker_id: {speaker_id}")
        
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
            
        # Check if engine is still running
        if self.process and self.process.poll() is not None:
            print(f"[AivisSpeech] Engine process died (exit code: {self.process.returncode})")
            # Try to reinitialize
            print("[AivisSpeech] Attempting to restart engine...")
            if self.start_engine() and await self.initialize():
                print("[AivisSpeech] Engine restarted successfully")
            else:
                print("[AivisSpeech] Failed to restart engine")
                return None
        
        # Retry logic for connection errors
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Test connection first
                try:
                    print(f"[AivisSpeech] 接続テスト中... (試行 {attempt + 1}/{max_retries})")
                    test_response = await self.client.get("/version")
                    if test_response.status_code != 200:
                        raise Exception(f"Engine not responding (status: {test_response.status_code})")
                    print(f"[AivisSpeech] 接続OK")
                except httpx.RequestError as e:
                    raise Exception(f"Cannot connect to engine: {e}")
                
                # Create audio query
                print(f"[AivisSpeech] Creating audio query - text: '{text}', speaker_id: {speaker_id}")
                
                # Add timeout for Windows
                query_start = asyncio.get_event_loop().time()
                try:
                    query_response = await asyncio.wait_for(
                        self.client.post(
                            "/audio_query",
                            params={"text": text, "speaker": speaker_id}
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
                
                # Synthesize audio
                synth_start = asyncio.get_event_loop().time()
                try:
                    synthesis_response = await asyncio.wait_for(
                        self.client.post(
                            "/synthesis",
                            params={"speaker": speaker_id},
                            json=audio_query,
                            headers={"Content-Type": "application/json"}
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
                error_msg = str(e)
                if ("connect" in error_msg.lower() or "server error" in error_msg.lower()) and attempt < max_retries - 1:
                    print(f"[AivisSpeech] Error (attempt {attempt + 1}/{max_retries}): {error_msg}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    
                    # Try to reinitialize client on connection errors
                    if "connect" in error_msg.lower():
                        try:
                            await self.cleanup()
                            await self.initialize()
                            print("[AivisSpeech] Client reinitialized")
                        except:
                            pass
                else:
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
        print(f"[AivisSpeech] synthesize_sync() called - text: '{text}', speaker_id: {speaker_id}")
        
        if self.process and self.process.poll() is not None:
            print(f"[AivisSpeech] Engine process died (exit code: {self.process.returncode})")
            return None
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Test connection first
                print(f"[AivisSpeech] Testing connection (attempt {attempt + 1}/{max_retries})")
                # Use longer timeout on Windows
                timeout = 10 if os.name == 'nt' else 5
                test_response = requests.get(f"{self.base_url}/version", timeout=timeout)
                if test_response.status_code != 200:
                    raise Exception(f"Engine not responding (status: {test_response.status_code})")
                print(f"[AivisSpeech] Connection OK")
                
                # Create audio query
                print(f"[AivisSpeech] Creating audio query - text: '{text}', speaker_id: {speaker_id}")
                # Use longer timeout on Windows (30s for audio query)
                timeout = 30 if os.name == 'nt' else 10
                query_response = requests.post(
                    f"{self.base_url}/audio_query",
                    params={"text": text, "speaker": speaker_id},
                    timeout=timeout
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
                synthesis_response = requests.post(
                    f"{self.base_url}/synthesis",
                    params={"speaker": speaker_id},
                    json=audio_query,
                    headers={"Content-Type": "application/json"},
                    timeout=timeout
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
                error_msg = str(e)
                if ("connect" in error_msg.lower() or "server error" in error_msg.lower()) and attempt < max_retries - 1:
                    print(f"[AivisSpeech] Error (attempt {attempt + 1}/{max_retries}): {error_msg}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
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
            response = await self.client.get("/speakers")
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