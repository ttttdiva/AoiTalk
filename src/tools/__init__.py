"""Tools package for voice assistant runtime."""

from .core import ToolDefinition, tool
from .registry import get_registry, init_global_tools_registry, register_tool

from .external import (
    MCPPlugin,
    call_mcp_tool,
    call_mcp_tool_async,
    create_mcp_tool_wrapper,
    set_mcp_plugin,
    use_mcp_tool,
)
from .image_generation import generate_image

try:
    from ..skills.executor import invoke_skill
except ImportError:
    invoke_skill = None

from .entertainment.spotify.auth import (
    get_spotify_manager,
    init_spotify_manager,
    set_spotify_auth_code,
    setup_spotify_auth,
)
from .entertainment import (
    add_queue_to_playlist,
    add_tracks_to_playlist,
    clear_spotify_queue,
    create_playlist,
    create_playlist_from_queue,
    find_and_play_spotify_music,
    get_spotify_status,
    get_spotify_user_playlists,
    pause_spotify,
    play_playlist,
    play_song_now,
    play_spotify_track,
    queue_song,
    remove_from_queue,
    remove_tracks_from_playlist,
    search_spotify_music,
    show_queue,
    skip_spotify_track,
)

_tools_to_register = [
    setup_spotify_auth,
    set_spotify_auth_code,
    generate_image,
]

for _tool_def in _tools_to_register:
    if isinstance(_tool_def, ToolDefinition):
        register_tool(_tool_def)

print(f"[Tools] {len(get_registry())} tools registered")

__all__ = [
    "ToolDefinition",
    "tool",
    "register_tool",
    "get_registry",
    "init_global_tools_registry",
    "search_spotify_music",
    "play_spotify_track",
    "pause_spotify",
    "skip_spotify_track",
    "get_spotify_status",
    "queue_song",
    "play_song_now",
    "show_queue",
    "clear_spotify_queue",
    "get_spotify_user_playlists",
    "setup_spotify_auth",
    "set_spotify_auth_code",
    "init_spotify_manager",
    "get_spotify_manager",
    "create_playlist_from_queue",
    "add_queue_to_playlist",
    "remove_tracks_from_playlist",
    "add_tracks_to_playlist",
    "create_playlist",
    "play_playlist",
    "remove_from_queue",
    "find_and_play_spotify_music",
    "use_mcp_tool",
    "call_mcp_tool",
    "call_mcp_tool_async",
    "create_mcp_tool_wrapper",
    "set_mcp_plugin",
    "MCPPlugin",
    "generate_image",
    "invoke_skill",
]
