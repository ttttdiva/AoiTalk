"""
Audio recording functionality for Voice Assistant
"""
import pyaudio
import numpy as np
import threading
import queue
import time
from typing import Optional, Callable


class AudioRecorder:
    """Audio recorder for capturing voice input"""
    
    def __init__(self, 
                 device_index: Optional[int] = None,
                 sample_rate: int = 16000,
                 chunk_size: int = 1024,
                 channels: int = 1,
                 format: int = pyaudio.paInt16):
        """Initialize audio recorder
        
        Args:
            device_index: Audio input device index
            sample_rate: Sample rate in Hz
            chunk_size: Number of frames per buffer
            channels: Number of audio channels
            format: Audio format
        """
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.channels = channels
        self.format = format
        
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.recording = False
        self.audio_queue = queue.Queue()
        self.record_thread = None
        
        # Voice detection parameters
        self.silence_threshold = 80   # RMS threshold for silence detection (lower for quieter speech)
        self.silence_duration = 1.5   # Seconds of silence before stopping (reduced for faster response)
        self.min_recording_duration = 0.2  # Minimum recording duration (reduced)
        self.max_recording_duration = 30.0  # Maximum recording duration
        self.voice_start_threshold = 120  # RMS threshold to start recording (lower for quieter speech)
        self.pre_recording_buffer = 0.5  # Seconds of audio to buffer before voice detection
        self.debug = True  # Enable debug logging
        
    def list_devices(self):
        """List available audio input devices"""
        info = self.audio.get_host_api_info_by_index(0)
        num_devices = info.get('deviceCount')
        
        devices = []
        for i in range(num_devices):
            device_info = self.audio.get_device_info_by_host_api_device_index(0, i)
            if device_info.get('maxInputChannels') > 0:
                devices.append({
                    'index': i,
                    'name': device_info.get('name'),
                    'channels': device_info.get('maxInputChannels')
                })
                
        return devices
        
    def _calculate_rms(self, data: bytes) -> float:
        """Calculate RMS (Root Mean Square) of audio data
        
        Args:
            data: Audio data bytes
            
        Returns:
            RMS value
        """
        # Check if data is empty
        if not data:
            return 0.0
            
        try:
            audio_data = np.frombuffer(data, dtype=np.int16)
            
            # Check if audio data is valid
            if len(audio_data) == 0:
                return 0.0
                
            # Calculate RMS
            rms = np.sqrt(np.mean(audio_data.astype(np.float64)**2))
            
            # Check for NaN and return 0 if invalid
            if np.isnan(rms):
                return 0.0
                
            return float(rms)
        except Exception as e:
            # Return 0 on any calculation error
            return 0.0
        
    def _record_thread(self, 
                      voice_callback: Optional[Callable[[float], None]] = None,
                      stop_on_silence: bool = True):
        """Recording thread function
        
        Args:
            voice_callback: Callback function that receives RMS values
            stop_on_silence: Whether to stop recording on silence
        """
        frames = []
        silence_start = None
        recording_start = time.time()
        voice_detected = False  # Track if any voice has been detected
        waiting_for_voice = True  # Wait for initial voice before recording
        voice_frames = []  # Store frames only after voice is detected
        pre_buffer = []  # Pre-recording buffer
        buffer_size = int(self.sample_rate * self.pre_recording_buffer / self.chunk_size)
        
        # Track RMS values for debugging
        rms_values = []
        
        while self.recording:
            try:
                data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                
                # Skip empty data
                if not data or len(data) == 0:
                    continue
                
                # Calculate RMS for voice activity detection
                rms = self._calculate_rms(data)
                rms_values.append(rms)
                
                # Keep only last 100 RMS values for average
                if len(rms_values) > 100:
                    rms_values.pop(0)
                
                # Calculate moving average RMS
                avg_rms = sum(rms_values) / len(rms_values) if rms_values else 0
                
                # Debug logging
                if self.debug:
                    print(f"\r[Recorder] RMS: {rms:6.2f} | Avg: {avg_rms:6.2f} | Threshold: {self.silence_threshold} | Voice: {'YES' if voice_detected else 'NO '}", end="", flush=True)
                
                # Call voice callback if provided
                if voice_callback:
                    voice_callback(rms)
                    
                # Voice activity detection
                if stop_on_silence:
                    if waiting_for_voice:
                        # Add to pre-buffer
                        pre_buffer.append(data)
                        if len(pre_buffer) > buffer_size:
                            pre_buffer.pop(0)
                        
                        # Check for voice
                        if rms >= self.voice_start_threshold or (avg_rms > self.silence_threshold and len(rms_values) > 10):
                            waiting_for_voice = False
                            voice_detected = True
                            # Add pre-buffer to voice frames
                            voice_frames.extend(pre_buffer)
                            voice_frames.append(data)
                            if self.debug:
                                print(f"\n[Recorder] Voice detected! RMS={rms:.2f}, Starting recording...")
                    else:
                        # Recording in progress
                        voice_frames.append(data)
                        
                        # Check for extended silence
                        if rms < self.silence_threshold and avg_rms < self.silence_threshold:
                            # Silence detected
                            if silence_start is None:
                                silence_start = time.time()
                            elif time.time() - silence_start > self.silence_duration:
                                # Stop recording after silence
                                recording_duration = time.time() - recording_start
                                if recording_duration >= self.min_recording_duration:
                                    if self.debug:
                                        print(f"\n[Recorder] Silence detected for {self.silence_duration}s, stopping...")
                                    self.recording = False
                                else:
                                    # Continue recording if too short
                                    silence_start = None
                        else:
                            # Voice continues
                            silence_start = None
                            
                        # Check max duration
                        if time.time() - recording_start > self.max_recording_duration:
                            if self.debug:
                                print(f"\n[Recorder] Max recording duration reached ({self.max_recording_duration}s)")
                            self.recording = False
                else:
                    # Not stopping on silence, just record everything
                    frames.append(data)
                        
            except Exception as e:
                print(f"\n[Recorder] Recording error: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                self.recording = False
                
        # Use voice_frames if we were waiting for voice, otherwise use all frames
        final_frames = voice_frames if stop_on_silence and voice_frames else frames
        
        # Put recorded audio in queue only if we have meaningful data
        if final_frames and len(final_frames) > 10:  # At least 10 chunks
            audio_data = b''.join(final_frames)
            # Check if audio has sufficient volume
            total_rms = self._calculate_rms(audio_data)
            if total_rms > 50 or not stop_on_silence:  # Lower threshold for final check
                if self.debug:
                    print(f"\n[Recorder] Putting audio in queue...")
                self.audio_queue.put(audio_data)
                if self.debug:
                    duration = time.time() - recording_start
                    print(f"[Recorder] Recording complete. Duration: {duration:.2f}s, Frames: {len(final_frames)}, Size: {len(audio_data)} bytes, RMS: {total_rms:.2f}")
                    print(f"[Recorder] Queue size after put: {self.audio_queue.qsize()}")
            else:
                if self.debug:
                    print(f"\n[Recorder] Audio too quiet. RMS: {total_rms:.2f}, discarding...")
        elif self.debug:
            print(f"\n[Recorder] No voice detected or insufficient data. Duration: {time.time() - recording_start:.2f}s")
            
    def start_recording(self, 
                       voice_callback: Optional[Callable[[float], None]] = None,
                       stop_on_silence: bool = True):
        """Start recording audio
        
        Args:
            voice_callback: Optional callback that receives RMS values
            stop_on_silence: Whether to stop recording on silence
        """
        if self.recording:
            return
            
        try:
            # Open audio stream
            self.stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_size
            )
        except Exception as e:
            print(f"Failed to open audio stream: {type(e).__name__}: {e}")
            if self.device_index is not None:
                print(f"Current device index: {self.device_index}")
                print("Available devices:")
                for device in self.list_devices():
                    print(f"  [{device['index']}] {device['name']}")
            raise
        
        # Clear queue
        while not self.audio_queue.empty():
            self.audio_queue.get()
            
        # Start recording thread
        self.recording = True
        self.record_thread = threading.Thread(
            target=self._record_thread,
            args=(voice_callback, stop_on_silence)
        )
        self.record_thread.start()
        
    def stop_recording(self) -> Optional[bytes]:
        """Stop recording and return audio data
        
        Returns:
            Recorded audio data as bytes, or None if no data
        """
        if not self.recording:
            return None
            
        # Stop recording
        self.recording = False
        
        # Wait for thread to finish
        if self.record_thread:
            self.record_thread.join()
            
        # Close stream
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            
        # Get recorded audio with timeout
        try:
            # Wait up to 1 second for audio data
            return self.audio_queue.get(block=True, timeout=1.0)
        except queue.Empty:
            if self.debug:
                print("[Recorder] No audio data in queue after recording")
            return None
            
    def _record_simple(self, voice_callback: Optional[Callable[[float], None]] = None) -> Optional[bytes]:
        """Simple recording method that works (based on test_direct_whisper.py)"""
        if self.debug:
            print("[Recorder] Starting simple recording...")
        
        try:
            # Open stream
            stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_size
            )
            
            frames = []
            silence_start = None
            start_time = time.time()
            voice_detected = False
            
            while True:
                data = stream.read(self.chunk_size, exception_on_overflow=False)
                frames.append(data)
                
                # Calculate RMS
                rms = self._calculate_rms(data)
                
                # Call callback
                if voice_callback:
                    voice_callback(rms)
                
                # Voice detection
                if rms > self.silence_threshold:
                    if not voice_detected and self.debug:
                        print(f"\n[Recorder] Voice detected! RMS={rms:.2f}")
                    voice_detected = True
                    silence_start = None
                elif voice_detected:
                    if silence_start is None:
                        silence_start = time.time()
                    elif time.time() - silence_start > self.silence_duration:
                        if self.debug:
                            print(f"\n[Recorder] Silence detected, stopping...")
                        break
                
                # Timeout
                if time.time() - start_time > self.max_recording_duration:
                    if self.debug:
                        print(f"\n[Recorder] Max duration reached")
                    break
            
            stream.stop_stream()
            stream.close()
            
            if not voice_detected:
                if self.debug:
                    print("[Recorder] No voice detected")
                return None
            
            audio_data = b''.join(frames)
            
            # Additional check for silence
            total_rms = self._calculate_rms(audio_data)
            if total_rms < 50:  # Too quiet
                if self.debug:
                    print(f"[Recorder] Audio too quiet (RMS: {total_rms:.2f}), discarding...")
                return None
                
            if self.debug:
                print(f"[Recorder] Recorded {len(audio_data)} bytes, {len(frames)} frames, RMS: {total_rms:.2f}")
            return audio_data
            
        except Exception as e:
            print(f"[Recorder] Error in simple recording: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def record_until_silence(self, 
                           voice_callback: Optional[Callable[[float], None]] = None) -> Optional[bytes]:
        """Record audio until silence is detected
        
        Args:
            voice_callback: Optional callback that receives RMS values
            
        Returns:
            Recorded audio data as bytes
        """
        # Use simple direct recording approach that works
        return self._record_simple(voice_callback)
        
    def record_for_duration(self, duration: float) -> Optional[bytes]:
        """Record audio for a specific duration
        
        Args:
            duration: Recording duration in seconds
            
        Returns:
            Recorded audio data as bytes
        """
        self.start_recording(stop_on_silence=False)
        time.sleep(duration)
        return self.stop_recording()
        
    def __del__(self):
        """Cleanup audio resources"""
        if self.recording:
            self.stop_recording()
        if self.audio:
            self.audio.terminate() 