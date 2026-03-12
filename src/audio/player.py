"""
Audio playback module with thread-safe operation
"""
import io
import wave
import threading
import time
from typing import Optional
import queue
import os

# Conditional PyAudio import for Docker/headless environments
_PYAUDIO_AVAILABLE = False
try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    print("[AudioPlayer] PyAudio not available - running in headless mode")


class AudioPlayer:
    """Audio player for playing synthesized speech with thread safety"""
    
    def __init__(self, headless: bool = None):
        """Initialize audio player
        
        Args:
            headless: If True, skip audio initialization (for Docker/server environments).
                     If None, auto-detect from environment variable AOITALK_HEADLESS.
        """
        # Auto-detect headless mode from environment
        if headless is None:
            headless = os.environ.get('AOITALK_HEADLESS', '').lower() in ('true', '1', 'yes')
        
        # Force headless if PyAudio is not available
        if not _PYAUDIO_AVAILABLE:
            headless = True
            
        self.headless = headless
        self.audio = None
        self.stream = None
        self.playing = False
        self.play_thread = None
        self.should_stop = False
        self.chunk_size = 1024
        
        # Thread safety
        self.lock = threading.RLock()  # Use RLock for Windows compatibility
        self.audio_queue = queue.Queue()
        
        # Windows-specific timeout settings
        self._stream_timeout = 2.0  # 2 second timeout for Windows
        self._cleanup_timeout = 1.0  # 1 second timeout for cleanup
        
        # Windows-specific resource management
        self._last_cleanup_time = 0
        self._force_reinit_threshold = 3  # Force reinit after 3 uses
        self._use_count = 0
        
        # Initialize PyAudio in a thread-safe manner (skip for headless)
        if self.headless:
            print("[AudioPlayer] Running in headless mode - audio playback disabled")
        else:
            self._init_audio()
    
    def _init_audio(self):
        """Initialize PyAudio safely with Windows-specific handling"""
        if self.headless or not _PYAUDIO_AVAILABLE:
            return
            
        with self.lock:
            if self.audio is None:
                try:
                    # Force garbage collection before initialization
                    import gc
                    gc.collect()
                    
                    self.audio = pyaudio.PyAudio()
                    self._use_count = 0
                    print("[AudioPlayer] PyAudio initialized successfully")
                except Exception as e:
                    print(f"[AudioPlayer] Failed to initialize PyAudio: {e}")
                    
    def _force_reinit(self):
        """Force PyAudio reinitialization for Windows resource management"""
        if self.headless or not _PYAUDIO_AVAILABLE:
            return
            
        with self.lock:
            try:
                # Cleanup existing resources
                if self.stream:
                    try:
                        self.stream.stop_stream()
                        self.stream.close()
                    except:
                        pass
                    self.stream = None
                
                if self.audio:
                    try:
                        self.audio.terminate()
                    except:
                        pass
                    self.audio = None
                
                # Force garbage collection
                import gc
                gc.collect()
                
                # Wait a bit for Windows to release resources
                time.sleep(0.1)
                
                # Reinitialize
                self.audio = pyaudio.PyAudio()
                self._use_count = 0
                print("[AudioPlayer] PyAudio reinitialized successfully")
                
            except Exception as e:
                print(f"[AudioPlayer] Failed to reinitialize PyAudio: {e}")
                self.audio = None
                    
    def play(self, audio_data: bytes, blocking: bool = True):
        """Play audio data with improved Windows resource management
        
        Args:
            audio_data: WAV format audio data
            blocking: Whether to block until playback completes
        """
        # Skip playback in headless mode
        if self.headless:
            print(f"[AudioPlayer] Headless mode - skipping playback of {len(audio_data)} bytes")
            return
            
        # Stop any current playback first
        if self.playing:
            self.stop()
            # Wait longer for cleanup to prevent ALSA resource conflicts
            time.sleep(0.2)
        
        # Windows-specific resource management
        self._use_count += 1
        current_time = time.time()
        
        # Force PyAudio reinitialization if threshold reached or too much time passed
        if (self._use_count >= self._force_reinit_threshold or 
            current_time - self._last_cleanup_time > 30):
            print(f"[AudioPlayer] Reinitializing PyAudio (use_count: {self._use_count})")
            self._force_reinit()
            self._last_cleanup_time = current_time
            
        if blocking:
            self._play_blocking(audio_data)
        else:
            self._play_async(audio_data)
            
    def _play_blocking(self, audio_data: bytes):
        """Play audio synchronously with interrupt support
        
        Args:
            audio_data: WAV format audio data
        """
        # Reset stop flag
        self.should_stop = False
        
        # Debug log on Windows
        if os.name == 'nt':
            print(f"[AudioPlayer] Starting playback - data size: {len(audio_data)} bytes")
        
        # Open WAV data
        audio_io = io.BytesIO(audio_data)
        
        stream = None
        try:
            with wave.open(audio_io, 'rb') as wav_file:
                # Get WAV parameters
                frames = wav_file.getnframes()
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                
                # Thread-safe stream creation with Windows timeout
                stream_created = False
                try:
                    with self.lock:
                        if self.audio is None:
                            self._init_audio()
                            
                        if self.audio is not None:
                            # Close any existing stream first with timeout
                            if self.stream is not None:
                                try:
                                    self.stream.stop_stream()
                                    self.stream.close()
                                except:
                                    pass
                                self.stream = None
                                # Small delay for Windows to release resources
                                time.sleep(0.05)
                                
                            # Open new stream with improved error handling and memory management
                            try:
                                # Force garbage collection before creating new stream
                                import gc
                                gc.collect()
                                
                                # Additional Windows-specific error handling
                                retry_count = 0
                                max_retries = 3
                                
                                while retry_count < max_retries:
                                    try:
                                        stream = self.audio.open(
                                            format=self.audio.get_format_from_width(sample_width),
                                            channels=channels,
                                            rate=sample_rate,
                                            output=True,
                                            frames_per_buffer=self.chunk_size,
                                            stream_callback=None  # Ensure no callback to prevent memory issues
                                        )
                                        self.stream = stream
                                        stream_created = True
                                        if os.name == 'nt':
                                            print(f"[AudioPlayer] Stream created - format: {sample_width}byte, {channels}ch, {sample_rate}Hz")
                                        print(f"[AudioPlayer] Stream created successfully (attempt {retry_count + 1})")
                                        break
                                    except Exception as stream_e:
                                        retry_count += 1
                                        print(f"[AudioPlayer] Stream creation failed (attempt {retry_count}): {stream_e}")
                                        if retry_count < max_retries:
                                            time.sleep(0.1)  # Wait before retry
                                        else:
                                            raise stream_e
                                            
                            except Exception as e:
                                print(f"[AudioPlayer] Failed to create stream after {max_retries} attempts: {e}")
                except Exception as lock_e:
                    print(f"[AudioPlayer] Lock acquisition error: {lock_e}")
                
                # Play audio in chunks for interrupt support
                if stream and stream_created:
                    remaining_frames = frames
                    while remaining_frames > 0 and not self.should_stop:
                        chunk_frames = min(self.chunk_size, remaining_frames)
                        data = wav_file.readframes(chunk_frames)
                        if data and not self.should_stop:
                            try:
                                stream.write(data)
                                remaining_frames -= chunk_frames
                                # Small delay for Windows to prevent audio glitches
                                time.sleep(0.001)
                            except Exception as e:
                                error_msg = str(e)
                                if any(err in error_msg for err in ['Unanticipated host error', 'ALSA', 'poll_descriptors']):
                                    print(f"[AudioPlayer] Audio system error during playback (continuing): {type(e).__name__}")
                                else:
                                    print(f"[AudioPlayer] Playback error: {e}")
                                    break
                        else:
                            break
                
        except Exception as e:
            print(f"[AudioPlayer] Audio playback error: {e}")
        finally:
            # Enhanced cleanup with timeout for Windows
            cleanup_start = time.time()
            try:
                with self.lock:
                    if stream:
                        try:
                            # Stop stream with timeout
                            stream.stop_stream()
                            # Give Windows time to stop
                            time.sleep(0.02)
                            stream.close()
                        except Exception as e:
                            print(f"[AudioPlayer] Stream cleanup error: {e}")
                            # Force close if needed
                            try:
                                stream.close()
                            except:
                                pass
                        
                    if self.stream == stream:
                        self.stream = None
                        
                    # Check cleanup timeout for Windows
                    if time.time() - cleanup_start > self._cleanup_timeout:
                        print(f"[AudioPlayer] Cleanup timeout on Windows")
                        
            except Exception as cleanup_e:
                print(f"[AudioPlayer] Final cleanup error: {cleanup_e}")
            
    def _play_async(self, audio_data: bytes):
        """Play audio asynchronously
        
        Args:
            audio_data: WAV format audio data
        """
        if self.playing:
            self.stop()
            
        self.play_thread = threading.Thread(
            target=self._play_thread_func,
            args=(audio_data,),
            daemon=True
        )
        self.play_thread.start()
        
    def _play_thread_func(self, audio_data: bytes):
        """Thread function for async playback
        
        Args:
            audio_data: WAV format audio data
        """
        self.playing = True
        
        try:
            self._play_blocking(audio_data)
        finally:
            self.playing = False
            
    def stop(self):
        """Stop current playback with improved error handling"""
        self.should_stop = True
        
        # Wait for playback to stop with shorter timeout for better responsiveness
        max_wait = 0.1  # Even shorter timeout for faster interrupt
        wait_interval = 0.01  # Check interval
        waited = 0
        
        while self.playing and waited < max_wait:
            time.sleep(wait_interval)
            waited += wait_interval
            
        # Enhanced stop logic with better error handling
        try:
            with self.lock:
                if self.stream:
                    try:
                        # Try graceful stop first
                        if hasattr(self.stream, 'is_active') and self.stream.is_active():
                            self.stream.stop_stream()
                            time.sleep(0.005)  # Very brief pause
                        
                        # Close stream
                        self.stream.close()
                        
                    except Exception as e:
                        # Handle specific ALSA/PyAudio errors gracefully
                        error_msg = str(e)
                        if any(err in error_msg for err in ['Unanticipated host error', 'ALSA', 'poll_descriptors']):
                            print(f"[AudioPlayer] Audio system error during stop (ignoring): {type(e).__name__}")
                        else:
                            print(f"[AudioPlayer] Stream stop error: {e}")
                        
                        # Force close regardless of error
                        try:
                            self.stream.close()
                        except:
                            pass
                    finally:
                        self.stream = None
                        
        except Exception as lock_e:
            print(f"[AudioPlayer] Critical error during stop: {lock_e}")
            # Force reset on critical errors
            self.stream = None
                
        self.playing = False
            
    def is_playing(self) -> bool:
        """Check if audio is currently playing
        
        Returns:
            True if playing
        """
        return self.playing
        
    def wait_until_done(self):
        """Wait until current playback finishes"""
        if self.play_thread and self.play_thread.is_alive():
            self.play_thread.join()
            
    def get_devices(self) -> list:
        """Get list of audio output devices
        
        Returns:
            List of output devices
        """
        devices = []
        
        with self.lock:
            if self.audio is None:
                self._init_audio()
                
            if self.audio:
                try:
                    info = self.audio.get_host_api_info_by_index(0)
                    num_devices = info.get('deviceCount')
                    
                    for i in range(num_devices):
                        device_info = self.audio.get_device_info_by_host_api_device_index(0, i)
                        if device_info.get('maxOutputChannels') > 0:
                            devices.append({
                                'index': i,
                                'name': device_info.get('name'),
                                'channels': device_info.get('maxOutputChannels')
                            })
                except Exception as e:
                    print(f"[AudioPlayer] Error getting devices: {e}")
                    
        return devices
        
    def __del__(self):
        """Cleanup audio resources"""
        try:
            self.stop()
        except:
            pass
        
        try:
            with self.lock:
                if self.audio:
                    try:
                        self.audio.terminate()
                    except:
                        pass
                    self.audio = None
        except:
            pass