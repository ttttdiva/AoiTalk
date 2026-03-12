"""
Tools package for voice assistant - unified tools and plugins

ClickUp, utility, web_search, workspace, memory_rag, os_operations, media は
MCP サーバーに移行済み。use_mcp_tool 経由でアクセスする。

直接登録ツール（MCP化対象外）:
  - generate_image: ホストプロセス結合
  - setup_spotify_auth / set_spotify_auth_code: ユーザー認証フロー
  - use_mcp_tool: MCPユニバーサルルーター
"""
# 新しいバックエンド非依存レジストリ
from .core import ToolDefinition, tool
from .registry import register_tool, get_registry

# ---- MCP化対象外ツール（ToolDefinition として直接登録） ----

# MCP統合（全MCPサーバーへのユニバーサルルーター）
from .external import use_mcp_tool, create_mcp_tool_wrapper, set_mcp_plugin, MCPPlugin

# 画像生成（ホストプロセス結合のためMCP化対象外）
from .image_generation import generate_image

# スキルシステム（LLM自動呼び出し用ツール）
try:
    from ..skills.executor import invoke_skill
except ImportError:
    invoke_skill = None

# Spotify認証（ユーザー認証フローのためMCP化対象外）
from .entertainment.spotify.auth import (
    setup_spotify_auth, set_spotify_auth_code,
    init_spotify_manager, get_spotify_manager,
)

# Spotifyツール（SpotifyAgent用 — MCP化対象外）
from .entertainment import (
    search_spotify_music,
    play_spotify_track,
    pause_spotify,
    skip_spotify_track,
    get_spotify_status,
    queue_song,
    play_song_now,
    show_queue,
    clear_spotify_queue,
    get_spotify_user_playlists,
    # プレイリスト管理機能
    create_playlist_from_queue,
    add_queue_to_playlist,
    remove_tracks_from_playlist,
    add_tracks_to_playlist,
    create_playlist,
    play_playlist,
    # キュー管理機能
    remove_from_queue,
    # 互換性のため古い名前も維持
    find_and_play_spotify_music,
)

# ---- ToolDefinition をグローバルレジストリに登録 ----

_tools_to_register = [
    setup_spotify_auth,
    set_spotify_auth_code,
    use_mcp_tool,
    generate_image,
    invoke_skill,
]

for _t in _tools_to_register:
    if isinstance(_t, ToolDefinition):
        register_tool(_t)

print(f"[Tools] {len(get_registry())}個のツールを直接登録しました（他はMCP経由）")

__all__ = [
    # 新しいレジストリ
    'ToolDefinition',
    'tool',
    'register_tool',
    'get_registry',
    # Spotify
    'search_spotify_music',
    'play_spotify_track',
    'pause_spotify',
    'skip_spotify_track',
    'get_spotify_status',
    'queue_song',
    'play_song_now',
    'show_queue',
    'clear_spotify_queue',
    'get_spotify_user_playlists',
    'setup_spotify_auth',
    'set_spotify_auth_code',
    'init_spotify_manager',
    'create_playlist_from_queue',
    'add_queue_to_playlist',
    'remove_tracks_from_playlist',
    'add_tracks_to_playlist',
    'create_playlist',
    'play_playlist',
    'remove_from_queue',
    'find_and_play_spotify_music',
    # MCP
    'use_mcp_tool',
    'create_mcp_tool_wrapper',
    'set_mcp_plugin',
    'MCPPlugin',
    # 画像生成
    'generate_image',
    # スキル
    'invoke_skill',
]
