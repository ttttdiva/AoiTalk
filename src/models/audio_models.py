"""
Pydantic models for audio configuration
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class RecorderConfig(BaseModel):
    """Configuration for audio recorder"""
    device_index: Optional[int] = Field(default=None, description="Audio device index")
    sample_rate: int = Field(default=16000, ge=8000, le=48000, description="Sample rate")
    chunk_size: int = Field(default=1024, ge=256, le=8192, description="Chunk size")
    channels: int = Field(default=1, ge=1, le=2, description="Number of channels")
    silence_threshold: float = Field(default=80.0, ge=0.0, description="Silence threshold")
    silence_duration: float = Field(default=1.5, ge=0.1, le=10.0, description="Silence duration")
    min_recording_duration: float = Field(default=0.2, ge=0.1, description="Minimum recording duration")
    max_recording_duration: float = Field(default=30.0, ge=1.0, description="Maximum recording duration")
    
    model_config = ConfigDict(extra="allow")


class VoiceConfig(BaseModel):
    """Configuration for voice processing"""
    input_device: Optional[int] = Field(default=None, description="Input device index")
    output_device: Optional[int] = Field(default=None, description="Output device index")
    sample_rate: int = Field(default=16000, ge=8000, le=48000, description="Sample rate")
    buffer_size: int = Field(default=1024, ge=256, le=8192, description="Buffer size")
    volume: float = Field(default=1.0, ge=0.0, le=2.0, description="Output volume")
    
    model_config = ConfigDict(extra="allow")


class AudioConfig(BaseModel):
    """Complete audio configuration"""
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    
    model_config = ConfigDict(extra="allow")