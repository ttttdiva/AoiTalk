"""
Improved audio buffering system for Discord voice
"""
import asyncio
import logging
import time
from typing import Dict, Optional, Callable
import numpy as np
from collections import deque

logger = logging.getLogger(__name__)


class AudioBuffer:
    """Advanced audio buffering with real-time processing"""
    
    def __init__(self, 
                 sample_rate: int = 48000,
                 channels: int = 2,
                 chunk_duration: float = 0.1,  # Process every 100ms
                 max_buffer_duration: float = 30.0,
                 speech_threshold: float = 20.0,
                 silence_threshold: float = 10.0,
                 silence_duration: float = 0.5,
                 min_speech_duration: float = 0.3):
        """
        Initialize audio buffer
        
        Args:
            sample_rate: Audio sample rate
            channels: Number of audio channels
            chunk_duration: Duration of audio chunks to process
            max_buffer_duration: Maximum buffer duration before forced processing
            speech_threshold: RMS threshold for speech detection
            silence_threshold: RMS threshold for silence detection
            silence_duration: Seconds of silence to end speech
            min_speech_duration: Minimum seconds of speech to process
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_duration = chunk_duration
        self.max_buffer_duration = max_buffer_duration
        self.speech_threshold = speech_threshold
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        
        # Calculate chunk size in bytes
        self.chunk_size = int(sample_rate * channels * 2 * chunk_duration)  # 2 bytes per sample
        
        # User buffers and state
        self.user_buffers: Dict[int, deque] = {}  # user_id -> deque of audio chunks
        self.user_states: Dict[int, Dict] = {}  # user_id -> state dict
        self.processing_tasks: Dict[int, asyncio.Task] = {}  # user_id -> processing task
        
    def get_user_state(self, user_id: int) -> Dict:
        """Get or create user state"""
        if user_id not in self.user_states:
            self.user_states[user_id] = {
                'is_speaking': False,
                'speech_start_time': None,
                'last_speech_time': None,
                'silence_start_time': None,
                'accumulated_audio': bytearray(),
                'last_process_time': time.time()
            }
        return self.user_states[user_id]
    
    def add_audio(self, user_id: int, audio_data: bytes):
        """Add audio data to user's buffer"""
        if user_id not in self.user_buffers:
            self.user_buffers[user_id] = deque()
        
        # Add audio to buffer
        self.user_buffers[user_id].append(audio_data)
        
        # Limit buffer size
        max_chunks = int(self.max_buffer_duration / self.chunk_duration)
        while len(self.user_buffers[user_id]) > max_chunks:
            self.user_buffers[user_id].popleft()
    
    async def process_user_buffer(self, user_id: int, callback: Callable) -> Optional[bytes]:
        """Process user's audio buffer and detect complete speech"""
        state = self.get_user_state(user_id)
        current_time = time.time()
        
        # Check if enough time has passed since last processing
        if current_time - state['last_process_time'] < self.chunk_duration:
            return None
        
        state['last_process_time'] = current_time
        
        # Get audio chunks to process
        if user_id not in self.user_buffers or not self.user_buffers[user_id]:
            return None
        
        # Process available chunks
        chunks_to_process = []
        while self.user_buffers[user_id] and len(chunks_to_process) < 5:  # Process up to 500ms at once
            chunks_to_process.append(self.user_buffers[user_id].popleft())
        
        if not chunks_to_process:
            return None
        
        # Combine chunks
        audio_data = b''.join(chunks_to_process)
        
        # Calculate RMS
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        rms = np.sqrt(np.mean(audio_array.astype(float) ** 2))
        
        # Add to accumulated audio
        state['accumulated_audio'].extend(audio_data)
        
        # Speech detection state machine
        if rms > self.speech_threshold:
            if not state['is_speaking']:
                # Speech started
                state['is_speaking'] = True
                state['speech_start_time'] = current_time
                state['silence_start_time'] = None
                state['accumulated_audio'] = bytearray(audio_data)  # Start fresh buffer
                logger.info(f"🎤 User {user_id} started speaking (RMS: {rms:.1f})")
            else:
                # Continue accumulating speech
                state['last_speech_time'] = current_time
                state['silence_start_time'] = None
        
        elif state['is_speaking']:
            # Check for silence
            if rms < self.silence_threshold:
                if state['silence_start_time'] is None:
                    state['silence_start_time'] = current_time
                    logger.debug(f"User {user_id} silence detected (RMS: {rms:.1f})")
                
                # Check if silence duration exceeded
                silence_duration = current_time - state['silence_start_time']
                if silence_duration >= self.silence_duration:
                    # Speech ended
                    return await self._end_speech(user_id, state, callback)
            else:
                # Reset silence timer if sound detected
                state['silence_start_time'] = None
        
        # Check maximum duration
        if state['is_speaking'] and state['speech_start_time']:
            speech_duration = current_time - state['speech_start_time']
            if speech_duration >= self.max_buffer_duration:
                logger.warning(f"User {user_id} speech exceeded max duration")
                return await self._end_speech(user_id, state, callback)
        
        return None
    
    async def _end_speech(self, user_id: int, state: Dict, callback: Callable) -> Optional[bytes]:
        """End speech and trigger callback"""
        if not state['accumulated_audio']:
            return None
        
        speech_duration = time.time() - state['speech_start_time']
        
        if speech_duration < self.min_speech_duration:
            logger.info(f"User {user_id} speech too short ({speech_duration:.1f}s), ignoring")
            state['accumulated_audio'] = bytearray()
            state['is_speaking'] = False
            state['speech_start_time'] = None
            state['silence_start_time'] = None
            return None
        
        logger.info(f"🔚 User {user_id} finished speaking (duration: {speech_duration:.1f}s)")
        
        # Get complete audio
        complete_audio = bytes(state['accumulated_audio'])
        
        # Reset state
        state['accumulated_audio'] = bytearray()
        state['is_speaking'] = False
        state['speech_start_time'] = None
        state['silence_start_time'] = None
        
        # Trigger callback
        if callback:
            await callback(user_id, complete_audio)
        
        return complete_audio
    
    def cleanup_user(self, user_id: int):
        """Clean up user state and buffers"""
        if user_id in self.user_buffers:
            del self.user_buffers[user_id]
        if user_id in self.user_states:
            del self.user_states[user_id]
        if user_id in self.processing_tasks:
            task = self.processing_tasks[user_id]
            if not task.done():
                task.cancel()
            del self.processing_tasks[user_id]