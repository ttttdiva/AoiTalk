"""
Speech detection and silence detection for Discord voice
"""
import asyncio
import logging
import time
from typing import Dict, Optional
import numpy as np

logger = logging.getLogger(__name__)


class SpeechDetector:
    """Detect speech start/end based on audio energy and silence duration"""
    
    def __init__(self, 
                 sample_rate: int = 48000,
                 silence_threshold: float = 15.0,
                 speech_threshold: float = 30.0,
                 silence_duration: float = 0.5,  # 1.0秒から0.5秒に短縮
                 min_speech_duration: float = 0.5,
                 max_speech_duration: float = 30.0):
        """
        Initialize speech detector
        
        Args:
            sample_rate: Audio sample rate
            silence_threshold: RMS threshold for silence detection
            speech_threshold: RMS threshold for speech detection
            silence_duration: Seconds of silence to end speech
            min_speech_duration: Minimum seconds of speech to process
            max_speech_duration: Maximum seconds of speech before forced processing
        """
        self.sample_rate = sample_rate
        self.silence_threshold = silence_threshold
        self.speech_threshold = speech_threshold
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        self.max_speech_duration = max_speech_duration
        
        # User state tracking
        self.user_states: Dict[int, Dict] = {}  # user_id -> state dict
        
    def get_user_state(self, user_id: int) -> Dict:
        """Get or create user state"""
        if user_id not in self.user_states:
            self.user_states[user_id] = {
                'is_speaking': False,
                'speech_start_time': None,
                'last_speech_time': None,
                'buffer': bytearray(),
                'silence_start_time': None
            }
        return self.user_states[user_id]
    
    def process_audio(self, user_id: int, audio_data: bytes) -> Optional[bytes]:
        """
        Process incoming audio and return complete speech when detected
        
        Args:
            user_id: User ID
            audio_data: Raw PCM audio data
            
        Returns:
            Complete speech audio data when speech ends, None otherwise
        """
        state = self.get_user_state(user_id)
        current_time = time.time()
        
        # Add to buffer
        state['buffer'].extend(audio_data)
        
        # Calculate RMS energy
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        rms = np.sqrt(np.mean(audio_array.astype(float) ** 2))
        
        # Check if user is speaking
        if rms > self.speech_threshold:
            if not state['is_speaking']:
                # Speech started
                state['is_speaking'] = True
                state['speech_start_time'] = current_time
                state['silence_start_time'] = None
                logger.info(f"🎤 User {user_id} started speaking (RMS: {rms:.1f})")
            
            state['last_speech_time'] = current_time
            state['silence_start_time'] = None  # Reset silence timer when speech detected
            
        elif state['is_speaking'] and rms < self.silence_threshold:
            # User was speaking but now silent
            if state['silence_start_time'] is None:
                state['silence_start_time'] = current_time
                logger.debug(f"User {user_id} silence started (RMS: {rms:.1f})")
            
            # Check if silence duration exceeded
            silence_duration = current_time - state['silence_start_time']
            if silence_duration >= self.silence_duration:
                # Speech ended
                logger.info(f"User {user_id} silence duration reached: {silence_duration:.2f}s")
                return self._end_speech(user_id, state)
        
        # Check maximum speech duration
        if state['is_speaking'] and state['speech_start_time']:
            speech_duration = current_time - state['speech_start_time']
            if speech_duration >= self.max_speech_duration:
                logger.warning(f"User {user_id} speech exceeded max duration, forcing end")
                return self._end_speech(user_id, state)
        
        return None
    
    def _end_speech(self, user_id: int, state: Dict) -> Optional[bytes]:
        """End speech and return buffer if valid"""
        if not state['buffer']:
            return None
        
        speech_duration = time.time() - state['speech_start_time']
        
        if speech_duration < self.min_speech_duration:
            logger.info(f"User {user_id} speech too short ({speech_duration:.1f}s), ignoring")
            state['buffer'] = bytearray()
            state['is_speaking'] = False
            state['speech_start_time'] = None
            state['silence_start_time'] = None
            return None
        
        logger.info(f"🔚 User {user_id} finished speaking (duration: {speech_duration:.1f}s, buffer size: {len(state['buffer'])} bytes)")
        
        # Get complete buffer
        complete_audio = bytes(state['buffer'])
        
        # Reset state
        state['buffer'] = bytearray()
        state['is_speaking'] = False
        state['speech_start_time'] = None
        state['silence_start_time'] = None
        
        return complete_audio
    
    def check_timeout(self, user_id: int) -> Optional[bytes]:
        """
        Check if user has timeout speech that needs to be processed
        
        Args:
            user_id: User ID
            
        Returns:
            Complete speech audio data if timeout, None otherwise
        """
        if user_id not in self.user_states:
            return None
            
        state = self.user_states[user_id]
        current_time = time.time()
        
        # Check if user has been silent for too long
        if state['is_speaking'] and state['last_speech_time']:
            time_since_last_speech = current_time - state['last_speech_time']
            
            # If no audio data received for silence duration, force end speech
            if time_since_last_speech >= self.silence_duration:
                logger.info(f"User {user_id} timeout detected after {time_since_last_speech:.2f}s of no data")
                return self._end_speech(user_id, state)
        
        return None
    
    def cleanup_user(self, user_id: int):
        """Clean up user state"""
        if user_id in self.user_states:
            del self.user_states[user_id]