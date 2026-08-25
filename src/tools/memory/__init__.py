"""
Memory tools for conversation history search and Spotify activity analysis
Note: Semantic memory (Mem0) now operates automatically without function calling
"""

from .memory_tools import semantic_memory_search
from .scoped_memory_tools import SCOPED_MEMORY_TOOLS
from ...features import Features

if not Features.is_enterprise():
    from .spotify_memory_tools import (
        search_spotify_activity,
        get_spotify_activity_stats,
        get_recent_spotify_activity,
        get_spotify_listening_patterns,
    )
else:
    def _spotify_memory_disabled(*_args, **_kwargs):
        return "Spotify memory tools are disabled in the Enterprise profile."

    search_spotify_activity = _spotify_memory_disabled
    get_spotify_activity_stats = _spotify_memory_disabled
    get_recent_spotify_activity = _spotify_memory_disabled
    get_spotify_listening_patterns = _spotify_memory_disabled

__all__ = [
    'semantic_memory_search',
    'SCOPED_MEMORY_TOOLS',
    'search_spotify_activity',
    'get_spotify_activity_stats', 
    'get_recent_spotify_activity',
    'get_spotify_listening_patterns'
]
