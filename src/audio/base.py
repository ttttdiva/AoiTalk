"""
Abstract interface for speech recognition engines
"""
from abc import ABC, abstractmethod
from typing import Optional, Generator, Tuple, Dict, Any


class SpeechRecognizerInterface(ABC):
    """Abstract interface for speech recognition engines"""
    
    @abstractmethod
    def recognize(self, 
                  audio_data: bytes, 
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,
                  prompt: Optional[str] = None) -> Optional[str]:
        """Recognize speech from audio data
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            language: Language code for recognition
            prompt: Optional prompt to guide recognition
            
        Returns:
            Recognized text or None if failed
        """
        pass
    
    @abstractmethod
    def start_stream(self) -> None:
        """Start a new streaming session"""
        pass
    
    @abstractmethod
    def process_audio_chunk(self, 
                           audio_data: bytes,
                           sample_rate: int = 16000,
                           channels: int = 1,
                           sample_width: int = 2) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """Process audio chunk and yield transcription results
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            
        Yields:
            Tuple of (is_final, text) where is_final indicates if the segment is complete
        """
        pass
    
    @abstractmethod
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and get final transcription
        
        Returns:
            Final transcription text or None
        """
        pass
    
    @abstractmethod
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        pass
    
    @abstractmethod
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        pass