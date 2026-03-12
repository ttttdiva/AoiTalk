"""
VOICEVOX TTS engine implementation
"""
import asyncio
import subprocess
import time
import os
import socket
import psutil
from typing import Optional, Dict, Any
import requests
from voicevox import Client
import aiohttp


class VoicevoxEngine:
    """VOICEVOX Text-to-Speech engine"""
    
    def __init__(self, engine_path: Optional[str] = None, host: str = "127.0.0.1", port: int = 50021):
        """Initialize VOICEVOX engine
        
        Args:
            engine_path: Path to VOICEVOX engine executable
            host: Host address for VOICEVOX server
            port: Port number for VOICEVOX server
        """
        self.engine_path = engine_path
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.process = None
        self.client = None
        self.session = None  # aiohttp session for connection pooling
        
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
            
            # Wait for engine to start
            start_time = time.time()
            
            print("Waiting for VOICEVOX engine to start...")
            
            while True:
                # Check if process died
                if self.process.poll() is not None:
                    print(f"VOICEVOX engine failed to start (exit code: {self.process.returncode})")
                    return False
                
                # Test HTTP connection
                try:
                    response = requests.get(f"{self.base_url}/version", timeout=2)
                    if response.status_code == 200:
                        version_info = response.text.strip().replace('"', '')
                        elapsed_time = int(time.time() - start_time)
                        print(f"VOICEVOX engine started successfully after {elapsed_time} seconds")
                        print(f"Version: {version_info}")
                        return True
                except requests.exceptions.RequestException:
                    pass
                    
                time.sleep(1)
            
        except Exception as e:
            print(f"Failed to start VOICEVOX engine: {e}")
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
            self.client = Client(base_url=self.base_url)
            
            # Override client's session if possible
            if hasattr(self.client, '_session'):
                # Close default session if exists
                if self.client._session and not self.client._session.closed:
                    await self.client._session.close()
                self.client._session = self.session
            
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
            
        # Check if engine is still running
        if self.process and self.process.poll() is not None:
            print(f"[VOICEVOX] Engine process died (exit code: {self.process.returncode})")
            # Try to reinitialize
            print("[VOICEVOX] Attempting to restart engine...")
            if self.start_engine() and await self.initialize():
                print("[VOICEVOX] Engine restarted successfully")
            else:
                print("[VOICEVOX] Failed to restart engine")
                return None
            
        # Retry logic for connection errors
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Test connection first
                try:
                    response = requests.get(f"{self.base_url}/version", timeout=1)
                    if response.status_code != 200:
                        raise Exception(f"Engine not responding (status: {response.status_code})")
                except requests.exceptions.RequestException as e:
                    raise Exception(f"Cannot connect to engine: {e}")
                
                # Create audio query
                audio_query = await self.client.create_audio_query(text, speaker=speaker_id)
                
                # Apply voice parameters
                audio_query.speed_scale = speed
                audio_query.pitch_scale = pitch
                audio_query.intonation_scale = intonation
                audio_query.volume_scale = volume
                
                # Synthesize audio
                audio_data = await audio_query.synthesis(speaker=speaker_id)
                return audio_data
                
            except Exception as e:
                error_msg = str(e)
                if "ConnectError" in error_msg or "connection" in error_msg.lower():
                    if attempt < max_retries - 1:
                        print(f"[VOICEVOX] Connection error (attempt {attempt + 1}/{max_retries}): {error_msg}")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        
                        # Try to reinitialize client
                        try:
                            # Close existing client properly before creating new one
                            if self.client:
                                try:
                                    await self.client.close()
                                except:
                                    pass
                            
                            # Close and recreate session
                            if self.session and not self.session.closed:
                                await self.session.close()
                            
                            # Create new session with Windows-optimized settings
                            connector = aiohttp.TCPConnector(
                                limit=20,
                                limit_per_host=10,
                                ttl_dns_cache=300,
                                force_close=False,
                                enable_cleanup_closed=True
                            )
                            timeout = aiohttp.ClientTimeout(total=30, connect=5)
                            self.session = aiohttp.ClientSession(
                                connector=connector,
                                timeout=timeout,
                                headers={'Connection': 'keep-alive'}
                            )
                            
                            # Create new client
                            self.client = Client(base_url=self.base_url)
                            
                            # Override client's session if possible
                            if hasattr(self.client, '_session'):
                                if self.client._session and not self.client._session.closed:
                                    await self.client._session.close()
                                self.client._session = self.session
                                
                            print("[VOICEVOX] Client reinitialized")
                        except Exception as reinit_error:
                            print(f"[VOICEVOX] Failed to reinitialize client: {reinit_error}")
                    else:
                        print(f"[VOICEVOX] Synthesis error after {max_retries} attempts: {type(e).__name__}: {e}")
                        return None
                else:
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
            speakers = await self.client.fetch_speakers()
            return speakers
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