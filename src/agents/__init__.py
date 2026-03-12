"""
Agent implementations for AoiTalk using OpenAI Agents SDK.

This module provides specialized agents that encapsulate domain-specific
functionality while reducing token consumption and improving organization.

Feature Flags control which agents are loaded.
"""
from src.features import Features

from .base import BaseAgent

# SpotifyAgent is only available if entertainment feature is enabled
if Features.entertainment():
    from .spotify_agent import SpotifyAgent
else:
    SpotifyAgent = None

__all__ = ["BaseAgent", "SpotifyAgent"]