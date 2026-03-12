"""
Pydantic models for configuration
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, field_validator, ConfigDict


class LLMConfig(BaseModel):
    """Configuration for LLM engines"""
    engine: str = Field(default="gemini", description="LLM engine name")
    model: str = Field(default="gemini-3-flash-preview", description="Model name")
    api_key: Optional[str] = Field(default=None, description="API key")
    max_tokens: int = Field(default=4096, ge=1, le=32768, description="Maximum tokens")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Temperature")
    top_p: float = Field(default=0.9, ge=0.0, le=1.0, description="Top-p sampling")
    
    model_config = ConfigDict(extra="allow")


class TTSConfig(BaseModel):
    """Configuration for TTS engines"""
    engine: str = Field(default="voicevox", description="TTS engine name")
    speaker_id: int = Field(default=3, ge=0, description="Speaker ID")
    speed: float = Field(default=1.0, ge=0.1, le=3.0, description="Speech speed")
    pitch: float = Field(default=0.0, ge=-1.0, le=1.0, description="Pitch adjustment")
    volume: float = Field(default=1.0, ge=0.0, le=2.0, description="Volume")
    enabled: bool = Field(default=True, description="Enable TTS")
    
    model_config = ConfigDict(extra="allow")


class SpeechRecognitionConfig(BaseModel):
    """Configuration for speech recognition"""
    engine: str = Field(default="whisper", description="Speech recognition engine")
    language: str = Field(default="ja", description="Language code")
    model: str = Field(default="small", description="Model size")
    hallucination_detection: bool = Field(default=True, description="Enable hallucination detection")
    min_audio_duration: float = Field(default=0.2, ge=0.1, description="Minimum audio duration")
    energy_threshold: float = Field(default=0.000005, ge=0.0, description="Energy threshold")
    
    model_config = ConfigDict(extra="allow")


class MemoryConfig(BaseModel):
    """Configuration for memory management"""
    enabled: bool = Field(default=True, description="Enable memory")
    enable_search: bool = Field(default=True, description="Enable search")
    embedding_model: str = Field(default="all-MiniLM-L6-v2", description="Embedding model")
    max_memory_items: int = Field(default=1000, ge=1, description="Maximum memory items")
    
    model_config = ConfigDict(extra="allow")


class BaseConfig(BaseModel):
    """Base configuration model"""
    mode: str = Field(default="terminal", description="Application mode")
    default_character: str = Field(default="ずんだもん", description="Default character")
    debug: bool = Field(default=False, description="Debug mode")
    
    # Sub-configurations
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    speech_recognition: SpeechRecognitionConfig = Field(default_factory=SpeechRecognitionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    
    model_config = ConfigDict(extra="allow")
        
    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v):
        valid_modes = ['terminal', 'voice_chat', 'discord']
        if v not in valid_modes:
            raise ValueError(f'Mode must be one of {valid_modes}')
        return v
