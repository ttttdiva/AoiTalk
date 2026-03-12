"""
Pydantic models for messages
"""

from typing import Optional, List, Dict, Any, Union, Literal
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, ConfigDict


class BaseMessage(BaseModel):
    """Base message model"""
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(default_factory=datetime.now, description="Message timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    
    model_config = ConfigDict(extra="allow")


class UserMessage(BaseMessage):
    """User message model"""
    type: Literal["user"] = Field(default="user", description="Message type")
    user_id: Optional[str] = Field(default=None, description="User ID")
    
    model_config = ConfigDict(extra="allow")


class AssistantMessage(BaseMessage):
    """Assistant message model"""
    type: Literal["assistant"] = Field(default="assistant", description="Message type")
    character: Optional[str] = Field(default=None, description="Character name")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Confidence score")
    
    model_config = ConfigDict(extra="allow")


class SystemMessage(BaseMessage):
    """System message model"""
    type: Literal["system"] = Field(default="system", description="Message type")
    level: str = Field(default="info", description="Message level")
    
    @field_validator('level')
    @classmethod
    def validate_level(cls, v):
        valid_levels = ['debug', 'info', 'warning', 'error']
        if v not in valid_levels:
            raise ValueError(f'Level must be one of {valid_levels}')
        return v
    
    model_config = ConfigDict(extra="allow")


class ChatMessage(BaseModel):
    """Generic chat message for API compatibility"""
    type: str = Field(..., description="Message type")
    message: str = Field(..., description="Message content")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(), description="ISO timestamp")
    character: Optional[str] = Field(default=None, description="Character name")
    
    @field_validator('type')
    @classmethod
    def validate_type(cls, v):
        valid_types = ['user', 'assistant', 'system']
        if v not in valid_types:
            raise ValueError(f'Type must be one of {valid_types}')
        return v
    
    model_config = ConfigDict(extra="allow")


class VoiceStatus(BaseModel):
    """Voice status model for WebSocket communication"""
    ready: bool = Field(..., description="Voice system ready")
    rms: float = Field(default=0.0, ge=0.0, description="RMS value")
    recording: bool = Field(default=False, description="Recording status")
    
    model_config = ConfigDict(extra="allow")