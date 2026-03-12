"""
A.I.VOICE TTS engine implementation using pythonnet
注意: このエンジンはWindowsでのみ動作します

A.I.VOICEはAITalk®技術を基盤とした音声合成エンジンです。
pythonnetを使用して.NET APIにアクセスします。
"""
import platform
import os
import subprocess
import time
from typing import Optional, List, Dict, Any
import warnings
import tempfile
import threading


# Windows環境でのみpythonnetをインポート
if platform.system() == "Windows":
    try:
        import clr
        AIVOICE_AVAILABLE = True
    except ImportError:
        AIVOICE_AVAILABLE = False
        clr = None
else:
    AIVOICE_AVAILABLE = False
    clr = None


class AIVoiceEngine:
    """A.I.VOICE Text-to-Speech engine (Windows only)"""
    
    def __init__(self, aivoice_path: str = r"C:\Program Files\AI\AIVoice\AIVoiceEditor\AIVoiceEditor.exe"):
        """Initialize A.I.VOICE engine
        
        Args:
            aivoice_path: Path to A.I.VOICE executable
        """
        self.aivoice_path = aivoice_path
        self.aivoice_dir = os.path.dirname(aivoice_path) if aivoice_path else None
        self.tts_control = None
        self.is_initialized = False
        self.available_voices = []
        self.current_voice = None
        self.process = None
        self.connection_method = None  # 'api', 'com', 'file'
        
        # 可能なAPI DLLパス
        self.possible_api_paths = [
            "AI.Talk.Editor.Api.dll",
            "AITalk.Api.dll", 
            "AIVoice.Api.dll",
            "AI.Talk.dll"
        ]
        
        if not AIVOICE_AVAILABLE:
            if platform.system() != "Windows":
                warnings.warn("A.I.VOICE is only available on Windows platforms")
            else:
                warnings.warn("pythonnet library not found. Please install pythonnet")
    
    def _find_api_dll(self) -> Optional[str]:
        """Find A.I.VOICE API DLL
        
        Returns:
            Path to API DLL if found
        """
        if not self.aivoice_dir or not os.path.exists(self.aivoice_dir):
            return None
            
        # Check in A.I.VOICE installation directory
        for dll_name in self.possible_api_paths:
            dll_path = os.path.join(self.aivoice_dir, dll_name)
            if os.path.exists(dll_path):
                print(f"[A.I.VOICE] Found API DLL: {dll_path}")
                return dll_path
        
        # Check subdirectories
        for root, dirs, files in os.walk(self.aivoice_dir):
            for dll_name in self.possible_api_paths:
                if dll_name in files:
                    dll_path = os.path.join(root, dll_name)
                    print(f"[A.I.VOICE] Found API DLL: {dll_path}")
                    return dll_path
        
        return None
    
    def _start_aivoice_process(self) -> bool:
        """Start A.I.VOICE process if not already running
        
        Returns:
            True if process started successfully
        """
        try:
            # Check if A.I.VOICE is already running
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq AIVoiceEditor.exe'],
                capture_output=True, text=True, shell=True
            )
            
            if "AIVoiceEditor.exe" not in result.stdout:
                if not os.path.exists(self.aivoice_path):
                    print(f"[A.I.VOICE] Executable not found: {self.aivoice_path}")
                    return False
                
                print("[A.I.VOICE] Starting A.I.VOICE process...")
                self.process = subprocess.Popen([self.aivoice_path])
                
                # Wait for A.I.VOICE to start
                for i in range(30):  # Wait up to 30 seconds
                    time.sleep(1)
                    result = subprocess.run(
                        ['tasklist', '/FI', 'IMAGENAME eq AIVoiceEditor.exe'],
                        capture_output=True, text=True, shell=True
                    )
                    if "AIVoiceEditor.exe" in result.stdout:
                        print(f"[A.I.VOICE] Process started successfully after {i+1} seconds")
                        time.sleep(3)  # Additional wait for initialization
                        return True
                
                print("[A.I.VOICE] Failed to start process within timeout")
                return False
            else:
                print("[A.I.VOICE] Process already running")
                return True
                
        except Exception as e:
            print(f"[A.I.VOICE] Failed to start process: {type(e).__name__}: {e}")
            return False
    
    def _try_api_connection(self) -> bool:
        """Try to connect using .NET API
        
        Returns:
            True if API connection successful
        """
        try:
            api_dll_path = self._find_api_dll()
            if not api_dll_path:
                print("[A.I.VOICE] No API DLL found")
                return False
            
            # Add .NET references
            clr.AddReference("System")
            clr.AddReference(api_dll_path)
            
            # Try different possible namespaces
            possible_imports = [
                ("AI.Talk.Editor.Api", ["TtsControl", "HostStatus"]),
                ("AI.Talk.Api", ["TtsControl", "HostStatus"]),
                ("AITalk.Api", ["TtsControl", "HostStatus"]),
                ("AI.Talk", ["TtsControl", "HostStatus"]),
                ("AIVoice.Api", ["TtsControl", "HostStatus"])
            ]
            
            for namespace, classes in possible_imports:
                try:
                    # Dynamic import
                    module = __import__(namespace, fromlist=classes)
                    TtsControl = getattr(module, 'TtsControl')
                    
                    # Initialize TTS control
                    self.tts_control = TtsControl()
                    
                    # Try to get available hosts
                    hosts = self.tts_control.GetAvailableHostNames()
                    if hosts and len(hosts) > 0:
                        host_name = hosts[0]
                        result = self.tts_control.Initialize(host_name)
                        
                        if result == 0:  # Success
                            # Wait for ready status
                            for _ in range(10):
                                if hasattr(self.tts_control, 'Status'):
                                    status = getattr(module, 'HostStatus', None)
                                    if status and self.tts_control.Status == status.Ready:
                                        break
                                time.sleep(0.5)
                            
                            self.connection_method = 'api'
                            print(f"[A.I.VOICE] API connection successful via {namespace}")
                            return True
                    
                except (ImportError, AttributeError) as e:
                    continue
                except Exception as e:
                    print(f"[A.I.VOICE] API connection failed for {namespace}: {e}")
                    continue
            
            return False
            
        except Exception as e:
            print(f"[A.I.VOICE] API connection error: {type(e).__name__}: {e}")
            return False
    
    def _try_com_connection(self) -> bool:
        """Try to connect using COM interface
        
        Returns:
            True if COM connection successful
        """
        try:
            # Try COM connection
            import win32com.client
            
            com_names = [
                "AIVoice.Application",
                "AI.Talk.Application", 
                "AITalk.Application"
            ]
            
            for com_name in com_names:
                try:
                    self.tts_control = win32com.client.Dispatch(com_name)
                    self.connection_method = 'com'
                    print(f"[A.I.VOICE] COM connection successful: {com_name}")
                    return True
                except Exception:
                    continue
            
            return False
            
        except ImportError:
            print("[A.I.VOICE] pywin32 not available for COM connection")
            return False
        except Exception as e:
            print(f"[A.I.VOICE] COM connection error: {type(e).__name__}: {e}")
            return False
    
    def _try_file_connection(self) -> bool:
        """Try file-based connection (fallback)
        
        Returns:
            True if file connection setup successful
        """
        try:
            # File-based approach as fallback
            self.temp_dir = tempfile.mkdtemp(prefix="aivoice_")
            self.connection_method = 'file'
            print("[A.I.VOICE] Using file-based connection (fallback)")
            return True
        except Exception as e:
            print(f"[A.I.VOICE] File connection error: {type(e).__name__}: {e}")
            return False
    
    async def initialize(self) -> bool:
        """Initialize A.I.VOICE engine
        
        Returns:
            True if initialization successful
        """
        if not AIVOICE_AVAILABLE:
            print("[A.I.VOICE] Engine not available (Windows/pythonnet required)")
            return False
        
        try:
            # Start A.I.VOICE process
            if not self._start_aivoice_process():
                return False
            
            # Try different connection methods
            connection_methods = [
                ("API", self._try_api_connection),
                ("COM", self._try_com_connection),
                ("File", self._try_file_connection)
            ]
            
            for method_name, method_func in connection_methods:
                print(f"[A.I.VOICE] Trying {method_name} connection...")
                if method_func():
                    print(f"[A.I.VOICE] {method_name} connection established")
                    break
            else:
                print("[A.I.VOICE] All connection methods failed")
                return False
            
            # Load available voices
            self._load_available_voices()
            
            self.is_initialized = True
            print(f"[A.I.VOICE] Initialized successfully with {len(self.available_voices)} voices using {self.connection_method} method")
            return True
            
        except Exception as e:
            print(f"[A.I.VOICE] Initialization failed: {type(e).__name__}: {e}")
            self.cleanup()
            return False
    
    def _load_available_voices(self):
        """Load available voice list"""
        try:
            self.available_voices = []
            
            if self.connection_method == 'api' and self.tts_control:
                # API method
                if hasattr(self.tts_control, 'VoicePresetNames'):
                    voice_presets = self.tts_control.VoicePresetNames
                    self.available_voices = list(voice_presets) if voice_presets else []
                elif hasattr(self.tts_control, 'GetVoiceList'):
                    self.available_voices = list(self.tts_control.GetVoiceList())
                elif hasattr(self.tts_control, 'Voices'):
                    voices = self.tts_control.Voices
                    self.available_voices = [voice.Name for voice in voices] if voices else []
                    
            elif self.connection_method == 'com' and self.tts_control:
                # COM method
                try:
                    self.available_voices = list(self.tts_control.VoiceList)
                except:
                    try:
                        self.available_voices = [voice.Name for voice in self.tts_control.Voices]
                    except:
                        self.available_voices = ["デフォルト音声"]
                        
            elif self.connection_method == 'file':
                # File method - use default voices
                self.available_voices = ["デフォルト音声"]
            
            if len(self.available_voices) == 0:
                print("[A.I.VOICE] No voice presets found, using default")
                self.available_voices = ["デフォルト音声"]
            else:
                print(f"[A.I.VOICE] Found {len(self.available_voices)} voice presets:")
                for i, voice in enumerate(self.available_voices):
                    print(f"  {i}: {voice}")
                    
        except Exception as e:
            print(f"[A.I.VOICE] Failed to load voices: {type(e).__name__}: {e}")
            self.available_voices = ["デフォルト音声"]
    
    def set_voice(self, voice_name: str) -> bool:
        """Set voice by name"""
        if not self.is_initialized:
            return False
        
        try:
            if voice_name in self.available_voices:
                if self.connection_method == 'api' and self.tts_control:
                    if hasattr(self.tts_control, 'CurrentVoicePresetName'):
                        self.tts_control.CurrentVoicePresetName = voice_name
                    elif hasattr(self.tts_control, 'SetVoice'):
                        self.tts_control.SetVoice(voice_name)
                        
                elif self.connection_method == 'com' and self.tts_control:
                    if hasattr(self.tts_control, 'CurrentVoice'):
                        self.tts_control.CurrentVoice = voice_name
                
                self.current_voice = voice_name
                return True
            else:
                print(f"[A.I.VOICE] Voice not found: {voice_name}")
                return False
                
        except Exception as e:
            print(f"[A.I.VOICE] Failed to set voice: {type(e).__name__}: {e}")
            return False
    
    def set_voice_by_index(self, voice_index: int) -> bool:
        """Set voice by index"""
        if not self.is_initialized:
            return False
        
        if 0 <= voice_index < len(self.available_voices):
            return self.set_voice(self.available_voices[voice_index])
        return False
    
    async def synthesize(self, 
                        text: str,
                        voice_name: Optional[str] = None,
                        voice_index: Optional[int] = None,
                        speed: float = 1.0,
                        pitch: float = 1.0,
                        volume: float = 1.0,
                        intonation: float = 1.0,
                        **kwargs) -> Optional[bytes]:
        """Synthesize speech from text"""
        if not self.is_initialized:
            print("[A.I.VOICE] Engine not initialized")
            return None
        
        try:
            # Set voice if specified
            if voice_name:
                self.set_voice(voice_name)
            elif voice_index is not None:
                self.set_voice_by_index(voice_index)
            elif not self.current_voice and len(self.available_voices) > 0:
                self.set_voice_by_index(0)
            
            # Set parameters
            if self.connection_method == 'api' and self.tts_control:
                return await self._synthesize_api(text, speed, pitch, volume, intonation)
            elif self.connection_method == 'com' and self.tts_control:
                return await self._synthesize_com(text, speed, pitch, volume, intonation)
            elif self.connection_method == 'file':
                return await self._synthesize_file(text, speed, pitch, volume, intonation)
            else:
                print("[A.I.VOICE] No valid connection method")
                return None
                
        except Exception as e:
            print(f"[A.I.VOICE] Synthesis error: {type(e).__name__}: {e}")
            return None
    
    async def _synthesize_api(self, text: str, speed: float, pitch: float, volume: float, intonation: float) -> Optional[bytes]:
        """Synthesize using API method"""
        try:
            # Set parameters
            if hasattr(self.tts_control, 'Speed'):
                self.tts_control.Speed = max(0.5, min(2.0, speed))
            if hasattr(self.tts_control, 'Pitch'):
                self.tts_control.Pitch = max(0.5, min(2.0, pitch))
            if hasattr(self.tts_control, 'Volume'):
                self.tts_control.Volume = max(0.0, min(2.0, volume))
            if hasattr(self.tts_control, 'Intonation'):
                self.tts_control.Intonation = max(0.0, min(2.0, intonation))
            
            # Synthesize
            if hasattr(self.tts_control, 'SpeakToWaveData'):
                result = self.tts_control.SpeakToWaveData(text)
                if result and hasattr(result, 'WaveData') and result.WaveData:
                    return bytes(result.WaveData)
            elif hasattr(self.tts_control, 'TextToWave'):
                wave_data = self.tts_control.TextToWave(text)
                if wave_data:
                    return bytes(wave_data)
            
            return None
            
        except Exception as e:
            print(f"[A.I.VOICE] API synthesis error: {type(e).__name__}: {e}")
            return None
    
    async def _synthesize_com(self, text: str, speed: float, pitch: float, volume: float, intonation: float) -> Optional[bytes]:
        """Synthesize using COM method"""
        try:
            # Set parameters if available
            if hasattr(self.tts_control, 'Speed'):
                self.tts_control.Speed = speed
            if hasattr(self.tts_control, 'Pitch'):
                self.tts_control.Pitch = pitch
            if hasattr(self.tts_control, 'Volume'):
                self.tts_control.Volume = volume
            
            # Synthesize
            if hasattr(self.tts_control, 'SpeakToFile'):
                temp_file = os.path.join(self.temp_dir, "temp.wav")
                self.tts_control.SpeakToFile(text, temp_file)
                
                if os.path.exists(temp_file):
                    with open(temp_file, 'rb') as f:
                        wave_data = f.read()
                    os.remove(temp_file)
                    return wave_data
            
            return None
            
        except Exception as e:
            print(f"[A.I.VOICE] COM synthesis error: {type(e).__name__}: {e}")
            return None
    
    async def _synthesize_file(self, text: str, speed: float, pitch: float, volume: float, intonation: float) -> Optional[bytes]:
        """Synthesize using file method (fallback)"""
        try:
            # File-based synthesis as last resort
            print("[A.I.VOICE] File-based synthesis not fully implemented")
            return None
            
        except Exception as e:
            print(f"[A.I.VOICE] File synthesis error: {type(e).__name__}: {e}")
            return None
    
    def get_voices(self) -> List[str]:
        """Get list of available voices"""
        return self.available_voices.copy() if self.available_voices else []
    
    def get_current_voice(self) -> Optional[str]:
        """Get current voice name"""
        return self.current_voice
    
    def cleanup(self):
        """Cleanup A.I.VOICE resources"""
        try:
            if self.tts_control:
                if self.connection_method == 'api' and hasattr(self.tts_control, 'Terminate'):
                    self.tts_control.Terminate()
                elif self.connection_method == 'com' and hasattr(self.tts_control, 'Quit'):
                    self.tts_control.Quit()
                self.tts_control = None
            
            if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
                import shutil
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            
            # Don't terminate the A.I.VOICE process as user might be using it
            self.process = None
            
        except Exception as e:
            print(f"[A.I.VOICE] Cleanup error: {type(e).__name__}: {e}")
        
        self.is_initialized = False
        self.current_voice = None
        self.connection_method = None
    
    def __del__(self):
        """Cleanup on deletion"""
        self.cleanup()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()