"""
Custom AudioSink for discord-ext-voice-recv
"""

import struct
import logging
from discord.ext import voice_recv

logger = logging.getLogger(__name__)


class UserAudioSink(voice_recv.AudioSink):
    """Custom audio sink that processes audio per user"""
    
    def __init__(self, voice_handler):
        super().__init__()
        self.voice_handler = voice_handler
        
    def wants_opus(self):
        """Return False to receive PCM data instead of Opus"""
        return False
        
    def write(self, user, data):
        """Process audio data from a specific user
        
        Args:
            user: Discord user/member object
            data: VoiceData object containing PCM audio
        """
        logger.info(f"🎤 AudioSink.write called! User: {user.name if user else 'None'}")
        
        if not user:
            logger.warning("write() called with None user")
            return
            
        if user.bot:
            logger.debug(f"Skipping bot user: {user.name}")
            return
            
        # Get raw PCM data
        try:
            if hasattr(data, 'pcm'):
                pcm_data = data.pcm
            else:
                logger.error(f"Data object has no 'pcm' attribute. Type: {type(data)}")
                logger.error(f"Data attributes: {dir(data)}")
                return
        except Exception as e:
            logger.error(f"Error accessing PCM data: {e}")
            return
            
        # バイナリデータのログ出力を抑制
        logger.debug(f"✅ Received {len(pcm_data)} bytes of audio from {user.name}")
        
        # Call the voice handler's callback
        self.voice_handler._on_voice_receive(pcm_data, user)
        
    def cleanup(self):
        """Called when the sink is stopped"""
        logger.info("AudioSink cleanup called")