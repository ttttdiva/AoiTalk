"""
Assistant factory for creating assistant instances with appropriate audio interfaces
"""

from typing import Optional, Union
import discord
from ..config import Config
from ..session.manager import SessionManager
from ..audio.implementations.local import MicrophoneInput, SpeakerOutput
from ..audio.implementations.discord import VoiceChannelInput, VoiceChannelOutput
from .modes.voice_chat_mode_v2 import EnhancedVoiceChatMode
from .modes.terminal_mode import TerminalMode
from .base import BaseAssistant


class AssistantFactory:
    """Factory for creating assistant instances"""
    
    def __init__(self, config: Config, session_manager: SessionManager):
        """Initialize assistant factory
        
        Args:
            config: Configuration object
            session_manager: Session manager instance
        """
        self.config = config
        self.session_manager = session_manager
        
    async def create_voice_chat_assistant(self, 
                                        character: Optional[str] = None) -> BaseAssistant:
        """Create voice chat assistant with local audio
        
        Args:
            character: Character name to use
            
        Returns:
            Voice chat assistant instance
        """
        # Create local session
        session = await self.session_manager.create_local_session(character=character)
        
        # Create local audio interfaces
        audio_input = MicrophoneInput(
            sample_rate=self.config.get('speech_recognition.sample_rate', 16000)
        )
        audio_output = SpeakerOutput()
        
        # Create enhanced voice chat assistant
        assistant = EnhancedVoiceChatMode(
            config=self.config,
            audio_input=audio_input,
            audio_output=audio_output,
            session=session
        )
        
        return assistant
        
    async def create_discord_assistant(self,
                                     voice_client: discord.VoiceClient,
                                     user: discord.User,
                                     guild: Optional[discord.Guild] = None,
                                     channel: Optional[discord.VoiceChannel] = None,
                                     character: Optional[str] = None) -> BaseAssistant:
        """Create Discord assistant
        
        Args:
            voice_client: Discord voice client
            user: Discord user
            guild: Discord guild
            channel: Discord voice channel
            character: Character name to use
            
        Returns:
            Discord assistant instance
        """
        # Create Discord session
        session = await self.session_manager.create_discord_session(
            user=user,
            guild=guild,
            channel=channel,
            character=character
        )
        
        # Create Discord audio interfaces
        audio_input = VoiceChannelInput(
            voice_client=voice_client,
            user_id=user.id
        )
        audio_output = VoiceChannelOutput(voice_client=voice_client)
        
        # Create enhanced voice chat assistant (reuse for Discord)
        assistant = EnhancedVoiceChatMode(
            config=self.config,
            audio_input=audio_input,
            audio_output=audio_output,
            session=session
        )
        
        return assistant
        
    async def create_terminal_assistant(self,
                                      character: Optional[str] = None) -> BaseAssistant:
        """Create terminal mode assistant
        
        Args:
            character: Character name to use
            
        Returns:
            Terminal mode assistant instance
        """
        # Create local session
        session = await self.session_manager.create_local_session(character=character)
        
        # Terminal mode doesn't need audio interfaces
        # Use the original TerminalMode for now
        assistant = TerminalMode(self.config)
        
        # Store session reference
        assistant._session = session
        
        return assistant
        
    def create_legacy_assistant(self, mode: str) -> BaseAssistant:
        """Create legacy assistant without session management
        
        Args:
            mode: Assistant mode
            
        Returns:
            Legacy assistant instance
        """
        if mode == 'terminal':
            from .modes.terminal_mode import TerminalMode
            return TerminalMode(self.config)
        elif mode == 'voice_chat':
            from .modes.voice_chat_mode import VoiceChatMode
            return VoiceChatMode(self.config)
        else:
            raise ValueError(f"Unknown mode: {mode}")