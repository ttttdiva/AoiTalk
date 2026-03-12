"""
Enhanced base assistant class with unified audio interfaces
"""

import asyncio
import time
import platform
import os
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pathlib import Path

# WSL2環境の自動設定
if platform.system() == 'Linux':
    try:
        with open('/proc/version', 'r') as f:
            if 'microsoft' in f.read().lower():
                from dotenv import load_dotenv
                load_dotenv()
                
                pulse_runtime_path = os.getenv('PULSE_RUNTIME_PATH', '/mnt/wslg/runtime-dir/pulse')
                if os.path.exists(pulse_runtime_path):
                    os.environ['PULSE_RUNTIME_PATH'] = pulse_runtime_path
                
                os.environ['SDL_AUDIODRIVER'] = 'pulse'
    except:
        pass

from ..audio.interfaces import AudioInputInterface, AudioOutputInterface
from ..audio.pipeline.tts_pipeline import TTSPipeline
from ..session.base import BaseSession


class EnhancedBaseAssistant(ABC):
    """Enhanced base class for all assistant modes with unified audio interfaces"""
    
    def __init__(self, 
                 config,
                 mode: str,
                 audio_input: AudioInputInterface,
                 audio_output: AudioOutputInterface,
                 session: BaseSession):
        """Initialize enhanced base assistant
        
        Args:
            config: Configuration object
            mode: Assistant mode ('terminal', 'voice_chat', etc.)
            audio_input: Audio input interface implementation
            audio_output: Audio output interface implementation  
            session: Session object
        """
        self.config = config
        self.mode = mode
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.session = session
        self.running = False
        
        # Load character configuration from session or config
        self.character_name = session.character or self.config.default_character
        self.character_config = self.config.get_character_config(self.character_name)
        
        # Common initialization
        self._init_common_components()
        
    def _init_common_components(self):
        """Initialize components common to all modes"""
        # LLM client initialization
        from src.llm.manager import create_llm_client
        
        use_tools = self.config.get('use_tools', True)
        if use_tools:
            print("[ツールモード] Function calling・MCP対応")
        else:
            print("[標準モード] 基本的なLLMクライアントを使用します")
        
        self.llm_client = create_llm_client(self.config, use_agent=use_tools)
        
        # Set LLM system prompt
        personality = self.character_config.get('personality', {})
        system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
        self.llm_client.set_system_prompt(system_prompt)
        
        # Initialize TTS pipeline
        from src.tts.manager import TTSManager
        tts_manager = TTSManager(self.config.config)
        self.tts_pipeline = TTSPipeline(tts_manager, self.audio_output)
        
        # Initialize speech recognition (if needed)
        self.speech_recognition = None
        if self.mode in ['voice_chat', 'discord']:
            from src.audio.manager import SpeechRecognitionManager
            speech_config = self.config.get('speech_recognition', {})
            current_engine = speech_config.get('current_engine', 'whisper')
            self.speech_recognition = SpeechRecognitionManager(current_engine, speech_config)
        
    async def initialize(self) -> bool:
        """Initialize assistant components
        
        Returns:
            bool: True if initialization succeeded
        """
        print(f"初期化中... (キャラクター: {self.character_name})")
        
        # Initialize audio interfaces
        try:
            await self.audio_input.start()
            print("✓ 音声入力の初期化完了")
        except Exception as e:
            print(f"❌ 音声入力の初期化に失敗: {e}")
            return False
            
        # Mode-specific initialization
        return await self._initialize_mode_specific()
    
    @abstractmethod
    async def _initialize_mode_specific(self) -> bool:
        """Initialize mode-specific components
        
        Returns:
            bool: True if initialization succeeded
        """
        pass
    
    @abstractmethod
    async def run(self):
        """Run the assistant"""
        pass
        
    async def cleanup(self):
        """Cleanup resources"""
        self.running = False
        
        # Stop audio interfaces
        try:
            await self.audio_input.stop()
            await self.audio_output.stop()
        except Exception as e:
            print(f"Audio cleanup error: {e}")
        
        # Get goodbye message
        personality = self.character_config.get('personality', {})
        goodbye = personality.get('goodbyeReply', 'さようなら！')
        print(f"\n{goodbye}")
        
        # Mode-specific cleanup
        await self._cleanup_mode_specific()
        
    @abstractmethod
    async def _cleanup_mode_specific(self):
        """Cleanup mode-specific resources"""
        pass
        
    async def speak(self, text: str, 
                   speed: Optional[float] = None,
                   pitch: Optional[float] = None,
                   intonation: Optional[float] = None,
                   volume: Optional[float] = None) -> bool:
        """Speak text using TTS pipeline
        
        Args:
            text: Text to speak
            speed: Speech speed (0.5-2.0)
            pitch: Pitch (-300～300)
            intonation: Intonation (0.0-2.0)
            volume: Volume (0.0-1.0)
            
        Returns:
            bool: True if speech succeeded
        """
        # Get settings from session or use defaults
        speed = speed or self.session.get_setting('speech_rate', 1.0)
        pitch = pitch or self.session.get_setting('pitch', 0.0)
        intonation = intonation or self.session.get_setting('intonation', 1.0)
        volume = volume or self.session.get_setting('volume', 1.0)
        
        return await self.tts_pipeline.process_and_play(
            text=text,
            character=self.character_name,
            speed=speed,
            pitch=pitch,
            intonation=intonation,
            volume=volume
        )
        
    async def listen(self, timeout: Optional[float] = None) -> Optional[str]:
        """Listen for speech input
        
        Args:
            timeout: Timeout in seconds
            
        Returns:
            Recognized text or None
        """
        if not self.speech_recognition:
            return None
            
        # Read audio from input interface
        audio_data = await self.audio_input.read_audio(duration=timeout)
        
        if audio_data is None:
            return None
            
        # Process with speech recognition
        try:
            text = await self.speech_recognition.recognize_audio(audio_data)
            return text
        except Exception as e:
            print(f"Speech recognition error: {e}")
            return None
            
    def update_settings(self, settings: Dict[str, Any]):
        """Update session settings
        
        Args:
            settings: Dictionary of settings to update
        """
        for key, value in settings.items():
            self.session.set_setting(key, value)