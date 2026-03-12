"""
Memory tools for conversation history search and Spotify activity analysis
Note: Semantic memory (Mem0) now operates automatically without function calling
"""

from .memory_tools import search_memory
from .spotify_memory_tools import (
    search_spotify_activity,
    get_spotify_activity_stats,
    get_recent_spotify_activity,
    get_spotify_listening_patterns
)

__all__ = [
    'search_memory',
    'search_spotify_activity',
    'get_spotify_activity_stats', 
    'get_recent_spotify_activity',
    'get_spotify_listening_patterns'
]