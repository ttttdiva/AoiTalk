"""
CeVIO AI TTS engine implementation using SAPI5
注意: このエンジンはWindowsでのみ動作します

CeVIO AIは高品質な日本語音声合成エンジンです。
SAPI5インターフェースを使用してアクセスします。
"""
import platform
import time
from typing import Optional, List, Dict, Any
import warnings
import tempfile
import os

# Windows環境でのみwin32comをインポート
if platform.system() == "Windows":
    try:
        import win32com.client
        import pywintypes
        CEVIO_AVAILABLE = True
    except ImportError:
        CEVIO_AVAILABLE = False
        win32com = None
        pywintypes = None
else:
    CEVIO_AVAILABLE = False
    win32com = None
    pywintypes = None


class CevioEngine:
    """CeVIO AI Text-to-Speech engine (Windows only)"""
    
    def __init__(self):
        """Initialize CeVIO AI engine"""
        self.sapi = None
        self.is_initialized = False
        self.available_voices = []
        self.current_voice = None
        self.current_voice_index = None
        self.cevio_voices = []  # CeVIO AI specific voices
        
        if not CEVIO_AVAILABLE:
            if platform.system() != "Windows":
                warnings.warn("CeVIO AI is only available on Windows platforms")
            else:
                warnings.warn("win32com library not found. Please install pywin32")
    
    def _is_cevio_voice(self, voice_name: str) -> bool:
        """Check if voice is CeVIO AI voice
        
        Args:
            voice_name: Voice name to check
            
        Returns:
            True if voice is from CeVIO AI
        """
        cevio_indicators = [
            "cevio", "CeVIO", "CEVIO",
            "さとうささら", "すずきつづみ", "タカハシ",
            "ONE", "IA", "結月ゆかり", "紲星あかり",
            "東北きりたん", "東北イタコ", "東北ずん子",
            "音街ウナ", "ガール", "ボーイ"
        ]
        
        return any(indicator in voice_name for indicator in cevio_indicators)
    
    async def initialize(self) -> bool:
        """Initialize CeVIO AI engine
        
        Returns:
            True if initialization successful
        """
        if not CEVIO_AVAILABLE:
            print("[CeVIO AI] Engine not available (Windows/pywin32 required)")
            return False
        
        try:
            # Initialize SAPI5
            self.sapi = win32com.client.Dispatch("SAPI.SpVoice")
            
            if not self.sapi:
                print("[CeVIO AI] Failed to initialize SAPI5")
                return False
            
            # Get available voices
            voices = self.sapi.GetVoices()
            self.available_voices = []
            self.cevio_voices = []
            
            for i in range(voices.Count):
                voice = voices.Item(i)
                voice_name = voice.GetDescription()
                self.available_voices.append(voice_name)
                
                # Check if this is a CeVIO AI voice
                if self._is_cevio_voice(voice_name):
                    self.cevio_voices.append({
                        'index': i,
                        'name': voice_name,
                        'voice_object': voice
                    })
            
            if len(self.available_voices) == 0:
                print("[CeVIO AI] No SAPI5 voices found")
                return False
            
            if len(self.cevio_voices) == 0:
                print("[CeVIO AI] No CeVIO AI voices found in SAPI5")
                print("[CeVIO AI] Available voices:")
                for i, voice in enumerate(self.available_voices):
                    print(f"  {i}: {voice}")
                print("[CeVIO AI] Will use first available voice as fallback")
                # Use first available voice as fallback
                self.current_voice = self.available_voices[0]
                self.current_voice_index = 0
            else:
                print(f"[CeVIO AI] Found {len(self.cevio_voices)} CeVIO AI voices:")
                for cevio_voice in self.cevio_voices:
                    print(f"  {cevio_voice['index']}: {cevio_voice['name']}")
                
                # Set first CeVIO voice as default
                first_cevio = self.cevio_voices[0]
                self.current_voice = first_cevio['name']
                self.current_voice_index = first_cevio['index']
            
            self.is_initialized = True
            print(f"[CeVIO AI] Initialized successfully with {len(self.available_voices)} total voices")
            return True
            
        except Exception as e:
            print(f"[CeVIO AI] Initialization failed: {type(e).__name__}: {e}")
            if self.sapi:
                try:
                    self.sapi = None
                except:
                    pass
            return False
    
    def set_voice(self, voice_name: str) -> bool:
        """Set voice by name"""
        if not self.is_initialized or not self.sapi:
            return False
        
        try:
            if voice_name in self.available_voices:
                voice_index = self.available_voices.index(voice_name)
                return self.set_voice_by_index(voice_index)
            else:
                print(f"[CeVIO AI] Voice not found: {voice_name}")
                return False
                
        except Exception as e:
            print(f"[CeVIO AI] Failed to set voice: {type(e).__name__}: {e}")
            return False
    
    def set_voice_by_index(self, voice_index: int) -> bool:
        """Set voice by index"""
        if not self.is_initialized or not self.sapi:
            return False
        
        try:
            if 0 <= voice_index < len(self.available_voices):
                voices = self.sapi.GetVoices()
                voice = voices.Item(voice_index)
                self.sapi.Voice = voice
                
                self.current_voice = self.available_voices[voice_index]
                self.current_voice_index = voice_index
                
                print(f"[CeVIO AI] Voice set to: {self.current_voice}")
                return True
            else:
                print(f"[CeVIO AI] Invalid voice index: {voice_index}")
                return False
                
        except Exception as e:
            print(f"[CeVIO AI] Failed to set voice by index: {type(e).__name__}: {e}")
            return False
    
    def set_cevio_voice(self, cevio_voice_name: str) -> bool:
        """Set CeVIO AI specific voice by name
        
        Args:
            cevio_voice_name: Name of CeVIO AI voice
            
        Returns:
            True if voice set successfully
        """
        if not self.is_initialized:
            return False
        
        for cevio_voice in self.cevio_voices:
            if cevio_voice_name in cevio_voice['name'] or cevio_voice['name'] in cevio_voice_name:
                return self.set_voice_by_index(cevio_voice['index'])
        
        print(f"[CeVIO AI] CeVIO voice not found: {cevio_voice_name}")
        return False
    
    async def synthesize(self, 
                        text: str,
                        voice_name: Optional[str] = None,
                        voice_index: Optional[int] = None,
                        rate: int = 0,         # -10 to 10
                        volume: int = 100,     # 0 to 100
                        **kwargs) -> Optional[bytes]:
        """Synthesize speech from text using SAPI5
        
        Args:
            text: Text to synthesize
            voice_name: Optional voice name to use
            voice_index: Optional voice index to use
            rate: Speech rate (-10 to 10, default 0)
            volume: Volume (0 to 100, default 100)
            **kwargs: Additional parameters (ignored for SAPI5)
            
        Returns:
            WAV audio data as bytes, None on error
        """
        if not self.is_initialized or not self.sapi:
            print("[CeVIO AI] Engine not initialized")
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
            self.sapi.Rate = max(-10, min(10, rate))
            self.sapi.Volume = max(0, min(100, volume))
            
            # Create temporary file for audio output
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_path = temp_file.name
            
            try:
                # Create file stream
                file_stream = win32com.client.Dispatch("SAPI.SpFileStream")
                file_stream.Open(temp_path, 3)  # 3 = SSFMCreateForWrite
                
                # Set output to file
                original_output = self.sapi.AudioOutputStream
                self.sapi.AudioOutputStream = file_stream
                
                try:
                    # Speak to file
                    self.sapi.Speak(text, 0)  # 0 = synchronous
                    
                    # Wait for completion
                    self.sapi.WaitUntilDone(-1)
                    
                finally:
                    # Ensure proper cleanup regardless of errors
                    try:
                        # Close file stream first
                        file_stream.Close()
                    except:
                        pass
                    
                    # Restore original output
                    try:
                        self.sapi.AudioOutputStream = original_output
                    except:
                        pass
                
                # Read the generated file
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    with open(temp_path, 'rb') as f:
                        audio_data = f.read()
                    
                    # Clean up temporary file
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
                    
                    return audio_data
                else:
                    print("[CeVIO AI] No audio data generated")
                    return None
                    
            except Exception as e:
                print(f"[CeVIO AI] File synthesis error: {type(e).__name__}: {e}")
                # Clean up temporary file
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                except:
                    pass
                return None
                
        except Exception as e:
            print(f"[CeVIO AI] Synthesis error: {type(e).__name__}: {e}")
            return None
    
    def get_voices(self) -> List[str]:
        """Get list of all available SAPI5 voices"""
        return self.available_voices.copy() if self.available_voices else []
    
    def get_cevio_voices(self) -> List[Dict[str, Any]]:
        """Get list of CeVIO AI specific voices
        
        Returns:
            List of CeVIO voice info dictionaries
        """
        return [
            {
                'index': voice['index'],
                'name': voice['name']
            }
            for voice in self.cevio_voices
        ]
    
    def get_current_voice(self) -> Optional[str]:
        """Get current voice name"""
        return self.current_voice
    
    def is_cevio_voice_active(self) -> bool:
        """Check if current voice is a CeVIO AI voice"""
        if not self.current_voice:
            return False
        return self._is_cevio_voice(self.current_voice)
    
    def cleanup(self):
        """Cleanup CeVIO AI resources"""
        try:
            if self.sapi:
                # Reset to default output
                self.sapi.AudioOutputStream = None
                self.sapi = None
                
        except Exception as e:
            print(f"[CeVIO AI] Cleanup error: {type(e).__name__}: {e}")
        
        self.is_initialized = False
        self.current_voice = None
        self.current_voice_index = None
        self.available_voices = []
        self.cevio_voices = []
    
    def __del__(self):
        """Cleanup on deletion"""
        self.cleanup()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()