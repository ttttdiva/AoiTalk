"""
Spotify Agent implementation for AoiTalk.

This agent handles all Spotify-related operations, reducing token consumption
by consolidating 17 individual function tools into a single agent tool.
"""

from typing import Optional, Dict, Any, List
from agents import Agent, function_tool

from .base import BaseAgent


class SpotifyAgent(BaseAgent):
    """Specialized agent for Spotify music operations."""
    
    def __init__(self, model: str = "gpt-4o-mini"):
        """
        Initialize the Spotify agent.
        
        Args:
            model: The model to use (default: gpt-4o-mini for speed)
        """
        super().__init__(model)
    
    def _create_agent(self) -> Agent:
        """Create and configure the Spotify agent with all music tools."""
        # Create internal function tools for this agent only
        # This avoids global registration of these tools
        
        @function_tool
        def search_spotify_music(query: str, search_type: str = "track", limit: int = 5) -> str:
            """Spotifyで音楽を検索します"""
            # Import and call the implementation directly
            from ..tools.entertainment.spotify.search import search_spotify_music as original_func
            # Since original_func is decorated with @function_tool, we need to call it as a function
            # The OpenAI SDK handles the actual invocation
            return original_func(query=query, search_type=search_type, limit=limit)
        
        @function_tool
        def play_spotify_track(spotify_uri: str = None) -> str:
            """Spotifyで楽曲を即座に再生します"""
            from ..tools.entertainment.spotify import play_spotify_track as play_impl
            if hasattr(play_impl, '__wrapped__'):
                return play_impl.__wrapped__(spotify_uri)
            return play_impl(spotify_uri)
        
        @function_tool
        def pause_spotify() -> str:
            """Spotify再生を一時停止します"""
            from ..tools.entertainment.spotify import pause_spotify as pause_impl
            if hasattr(pause_impl, '__wrapped__'):
                return pause_impl.__wrapped__()
            return pause_impl()
        
        @function_tool
        def skip_spotify_track() -> str:
            """現在の曲をスキップして次の曲を再生します"""
            from ..tools.entertainment.spotify import skip_spotify_track as skip_impl
            if hasattr(skip_impl, '__wrapped__'):
                return skip_impl.__wrapped__()
            return skip_impl()
        
        @function_tool
        def get_spotify_status() -> str:
            """現在のSpotify再生状態を取得します"""
            from ..tools.entertainment.spotify import get_spotify_status as status_impl
            if hasattr(status_impl, '__wrapped__'):
                return status_impl.__wrapped__()
            return status_impl()
        
        @function_tool
        def queue_song(song_name: str) -> str:
            """曲名で検索してキューに追加します"""
            from ..tools.entertainment.spotify import queue_song as queue_impl
            if hasattr(queue_impl, '__wrapped__'):
                return queue_impl.__wrapped__(song_name)
            return queue_impl(song_name)
        
        @function_tool
        def play_song_now(song_name: str) -> str:
            """曲名で検索して今すぐ再生します"""
            from ..tools.entertainment.spotify import play_song_now as play_now_impl
            if hasattr(play_now_impl, '__wrapped__'):
                return play_now_impl.__wrapped__(song_name)
            return play_now_impl(song_name)
        
        @function_tool
        def show_queue() -> str:
            """現在のキュー（内部キュー + Spotifyキュー）を表示します"""
            from ..tools.entertainment.spotify import show_queue as show_impl
            if hasattr(show_impl, '__wrapped__'):
                return show_impl.__wrapped__()
            return show_impl()
        
        @function_tool
        def clear_spotify_queue() -> str:
            """すべてのキューをクリアします"""
            from ..tools.entertainment.spotify import clear_spotify_queue as clear_impl
            if hasattr(clear_impl, '__wrapped__'):
                return clear_impl.__wrapped__()
            return clear_impl()
        
        @function_tool
        def remove_from_queue(position: int) -> str:
            """指定した位置の曲をキューから削除します"""
            from ..tools.entertainment.spotify import remove_from_queue as remove_impl
            if hasattr(remove_impl, '__wrapped__'):
                return remove_impl.__wrapped__(position)
            return remove_impl(position)
        
        @function_tool
        def get_spotify_user_playlists() -> str:
            """ユーザーのSpotifyプレイリスト一覧を取得します"""
            from ..tools.entertainment.spotify import get_spotify_user_playlists as playlists_impl
            if hasattr(playlists_impl, '__wrapped__'):
                return playlists_impl.__wrapped__()
            return playlists_impl()
        
        @function_tool
        def create_playlist(name: str, description: str = "", public: bool = True) -> str:
            """新しいプレイリストを作成します"""
            from ..tools.entertainment.spotify import create_playlist as create_impl
            if hasattr(create_impl, '__wrapped__'):
                return create_impl.__wrapped__(name, description, public)
            return create_impl(name, description, public)
        
        @function_tool
        def create_playlist_from_queue(playlist_name: str) -> str:
            """現在のキューから新しいプレイリストを作成します"""
            from ..tools.entertainment.spotify import create_playlist_from_queue as create_queue_impl
            if hasattr(create_queue_impl, '__wrapped__'):
                return create_queue_impl.__wrapped__(playlist_name)
            return create_queue_impl(playlist_name)
        
        @function_tool
        def add_tracks_to_playlist(playlist_id: str, track_uris: List[str]) -> str:
            """プレイリストに楽曲を追加します"""
            from ..tools.entertainment.spotify import add_tracks_to_playlist as add_impl
            if hasattr(add_impl, '__wrapped__'):
                return add_impl.__wrapped__(playlist_id, track_uris)
            return add_impl(playlist_id, track_uris)
        
        @function_tool
        def add_queue_to_playlist(playlist_id: str) -> str:
            """現在のキューをプレイリストに追加します"""
            from ..tools.entertainment.spotify import add_queue_to_playlist as add_queue_impl
            if hasattr(add_queue_impl, '__wrapped__'):
                return add_queue_impl.__wrapped__(playlist_id)
            return add_queue_impl(playlist_id)
        
        @function_tool
        def remove_tracks_from_playlist(playlist_id: str, track_uris: List[str]) -> str:
            """プレイリストから楽曲を削除します"""
            from ..tools.entertainment.spotify import remove_tracks_from_playlist as remove_impl
            if hasattr(remove_impl, '__wrapped__'):
                return remove_impl.__wrapped__(playlist_id, track_uris)
            return remove_impl(playlist_id, track_uris)
        
        @function_tool
        def play_playlist(playlist_uri: str) -> str:
            """プレイリストを再生します"""
            from ..tools.entertainment.spotify import play_playlist as play_playlist_impl
            if hasattr(play_playlist_impl, '__wrapped__'):
                return play_playlist_impl.__wrapped__(playlist_uri)
            return play_playlist_impl(playlist_uri)
        
        # Create function tools list
        tools = [
            search_spotify_music,
            play_spotify_track,
            pause_spotify,
            skip_spotify_track,
            get_spotify_status,
            queue_song,
            play_song_now,
            show_queue,
            clear_spotify_queue,
            remove_from_queue,
            get_spotify_user_playlists,
            create_playlist,
            create_playlist_from_queue,
            add_tracks_to_playlist,
            add_queue_to_playlist,
            remove_tracks_from_playlist,
            play_playlist
        ]
        
        # Ensure all tools have proper names
        for tool in tools:
            if not hasattr(tool, 'name') and hasattr(tool, '__name__'):
                tool.name = tool.__name__
        
        instructions = """
あなたはSpotify音楽アシスタントです。ユーザーの音楽リクエストに対して適切に対応してください。

重要：音楽のリクエスト形式の理解と検索戦略
- 「○○の△△」形式の場合の検索手順：
  1. まず「○○ △△」（アーティスト名 曲名）として検索を試みる
  2. 結果が見つからない場合は「○○の△△」全体を曲名として再検索する
  例：「PogoのAlice」
    → 先に「Pogo Alice」で検索（アーティスト: Pogo、曲名: Alice）
    → 見つからなければ「PogoのAlice」で検索（曲名全体）
  例：「世界の終わり」
    → これは曲名の可能性が高いので、そのまま検索
- 「○○流して」「○○再生」→ 曲名「○○」として検索

主な機能：
1. **音楽検索と再生**: 
   - search_spotify_musicで検索してからplay_song_nowまたはqueue_songを使用
   - 「○○の△△」形式では2段階検索を実施
2. **再生制御**: 
   - 「今すぐ再生」「すぐに流して」「流して」→ play_song_now
   - 「キューに追加」「後で流して」→ queue_song
   - 「一時停止」「停止」→ pause_spotify
   - 「スキップ」「次の曲」→ skip_spotify_track
3. **キュー管理**:
   - 「キューを見せて」→ show_queue
   - 「キューをクリア」→ clear_spotify_queue
   - 「キューから削除」→ remove_from_queue
4. **プレイリスト**:
   - 「プレイリスト表示」→ get_spotify_user_playlists
   - 「プレイリスト作成」→ create_playlist
   - 「プレイリスト再生」→ play_playlist
   - キューからプレイリスト作成 → create_playlist_from_queue
5. **状態確認**: 「何が流れてる？」→ get_spotify_status

検索のベストプラクティス：
- 「の」が含まれる場合は、まずアーティストと曲名に分けて検索
- 見つからない場合は全体を曲名として再検索
- play_song_nowを使う場合は、検索クエリを工夫して最適な結果を得る

重要な注意事項：
- 自動キュー機能（「自動」と「キュー」が両方含まれる）はキーワード検知で別処理されるため、対応不要
- ユーザーの意図を理解し、適切なツールを選択する
- 音声対話を想定し、簡潔で聞き取りやすい応答を心がける
- 日本語で応答する

応答例：
- 「『{曲名}』を再生します」
- 「『{曲名}』をキューに追加しました」
- 「音楽を一時停止しました」
"""
        
        agent = Agent(
            name="SpotifyAssistant",
            model=self.model,
            instructions=instructions,
            tools=tools
        )
        
        print(f"[SpotifyAgent] Agent created with {len(tools)} tools")
        return agent
    
    def get_tool_name(self) -> str:
        """Get the name for this agent when used as a tool."""
        return "spotify_assistant"
    
    def get_tool_description(self) -> str:
        """Get the description for this agent when used as a tool."""
        return "Spotify音楽アシスタント - 音楽の検索、再生、キュー管理、プレイリスト操作を行います"