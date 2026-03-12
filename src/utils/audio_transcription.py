"""
Audio transcription using Gemini API

This module provides audio transcription functionality using Google's Gemini API.
Uses the existing GeminiLLMClient infrastructure - all Gemini API calls are delegated
to GeminiLLMClient to maintain single responsibility.
"""
from pathlib import Path
from typing import Optional, Union, Dict, Any
import os


class AudioTranscriber:
    """Audio transcription using Gemini 2.5 Flash
    
    This is a thin wrapper around GeminiLLMClient.transcribe_audio().
    All Gemini API communication is handled by GeminiLLMClient.
    """
    
    def __init__(self, config: Union[Dict[str, Any], Any] = None):
        """Initialize transcriber using existing GeminiLLMClient
        
        Args:
            config: Config dict or Config object with Gemini API key configured.
                   Can be a dict with 'gemini_api_key' key or a Config object.
        """
        if config is None:
            raise ValueError(
                "Config is required. "
                "AudioTranscriber must be initialized with a config dict or Config object."
            )
        
        # Import GeminiLLMClient here to use existing Gemini infrastructure
        from ..llm.gemini_engine import GeminiLLMClient
        
        # Get API key from config (works for both dict and Config object)
        api_key = None
        if isinstance(config, dict):
            api_key = config.get('gemini_api_key')
        else:
            # Assume Config object with get() method
            if hasattr(config, 'get'):
                api_key = config.get('gemini_api_key')
        
        # Fallback to environment variable
        if not api_key:
            api_key = os.getenv('GEMINI_API_KEY')
        
        if not api_key:
            raise ValueError(
                "Gemini API key not found. "
                "Ensure 'gemini_api_key' is in config or GEMINI_API_KEY is set in environment."
            )
        
        # Initialize GeminiLLMClient for transcription
        self.client = GeminiLLMClient(
            api_key=api_key,
            model="gemini-2.5-flash",
            config=config
        )
    
    def transcribe_audio(self, file_path: Union[str, Path]) -> Optional[str]:
        """Transcribe audio file using Gemini
        
        Args:
            file_path: Path to audio file
            
        Returns:
            Transcribed text or None if failed
        """
        return self.client.transcribe_audio(file_path)
