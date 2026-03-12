"""
Spotify統合モジュール - 分割されたモジュールの統合インターフェース
"""

# 認証関連
from .auth import (
    SpotifyManager,
    init_spotify_manager,
    get_spotify_manager,
    setup_spotify_auth,
    set_spotify_auth_code
)

# キューシステム
from .queue_system import (
    InternalQueue,
    get_internal_queue,
    queue_song,
    show_queue,
    clear_spotify_queue,
    remove_from_queue
)

# 再生制御
from .playback_control import (
    play_spotify_track,
    play_song_now,
    pause_spotify,
    skip_spotify_track,
    previous_track,
    add_to_queue,
    play_album,
    play_playlist
)

# 監視システム
from .monitoring import (
    start_auto_play_monitoring,
    stop_auto_play_monitoring,
    get_last_track_id,
    update_last_track_id,
    is_monitoring_active,
    is_skip_in_progress,
    set_skip_in_progress,
    request_monitor_reset
)

# 検索機能
from .search import (
    search_spotify_music,
    _validate_track
)

# プレイリスト管理
from .playlist_manager import (
    get_spotify_user_playlists,
    create_playlist,
    create_playlist_from_queue,
    add_tracks_to_playlist,
    add_queue_to_playlist,
    remove_tracks_from_playlist,
    add_playlist_to_queue
)

# 情報取得
from .info import (
    get_spotify_status,
    get_track_info,
    get_album_info,
    get_artist_info,
    get_playlist_info
)

# ユーティリティ
from .utilities import (
    reset_to_current_track,
    skip_all_queue,
    setup_spotify_auth_alias,
    set_spotify_auth_code_alias,
    add_song_to_queue,
    get_current_playing,
    spotify_pause,
    spotify_skip,
    spotify_previous
)

# 自動キュー管理はkeyword/spotifyに移動

# キーワード検出はkeyword/spotifyに移動

# 初期化処理
def initialize_spotify():
    """Spotifyモジュールを初期化"""
    result = init_spotify_manager()
    if result:
        print("[Spotify] モジュールが正常に初期化されました")
        # 自動再生監視を開始（デフォルトでは無効化）
        # start_auto_play_monitoring()
        # 自動キュー管理を初期化
        try:
            from ...keyword.spotify.auto_queue_manager import get_auto_queue_manager
            get_auto_queue_manager()
        except Exception as e:
            print(f"[Spotify] 自動初期化警告: {e}")
    else:
        print("[Spotify] モジュールの初期化に失敗しました")
    return result

# 互換性維持のためのグローバル変数エミュレーション
_spotify_manager = None
_internal_queue = None

def _get_spotify():
    """互換性維持：グローバルSpotifyクライアント取得"""
    manager = get_spotify_manager()
    if manager:
        return manager._get_spotify_client()
    return None

def _get_spotify_user():
    """互換性維持：グローバルユーザーSpotifyクライアント取得"""
    manager = get_spotify_manager()
    if manager:
        return manager._get_spotify_user_client()
    return None

# 追加の互換性エイリアス
def get_spotify_queue():
    """キュー表示（互換性維持）"""
    return show_queue()

def find_and_play_spotify_music(song_name: str):
    """楽曲検索と再生（互換性維持）"""
    return play_song_now(song_name)

def find_and_queue_spotify_music(song_name: str):
    """楽曲検索とキュー追加（互換性維持）"""
    return queue_song(song_name)

def queue_playlist(playlist_uri: str):
    """プレイリストをキューに追加（互換性維持）"""
    return add_playlist_to_queue(playlist_uri)

# すべての公開関数をエクスポート
__all__ = [
    # 認証関連
    'SpotifyManager',
    'init_spotify_manager',
    'get_spotify_manager', 
    'setup_spotify_auth',
    'set_spotify_auth_code',
    
    # キューシステム
    'InternalQueue',
    'get_internal_queue',
    'queue_song',
    'show_queue',
    'clear_spotify_queue',
    'remove_from_queue',
    
    # 再生制御
    'play_spotify_track',
    'play_song_now',
    'pause_spotify',
    'skip_spotify_track',
    'previous_track',
    'add_to_queue',
    'play_album',
    'play_playlist',
    
    # 監視システム
    'start_auto_play_monitoring',
    'stop_auto_play_monitoring',
    'get_last_track_id',
    'update_last_track_id',
    'is_monitoring_active',
    'is_skip_in_progress',
    'set_skip_in_progress',
    'request_monitor_reset',
    
    # 検索機能
    'search_spotify_music',
    '_validate_track',
    
    # プレイリスト管理
    'get_spotify_user_playlists',
    'create_playlist',
    'create_playlist_from_queue',
    'add_tracks_to_playlist',
    'add_queue_to_playlist',
    'remove_tracks_from_playlist',
    'add_playlist_to_queue',
    
    # 情報取得
    'get_spotify_status',
    'get_track_info',
    'get_album_info',
    'get_artist_info',
    'get_playlist_info',
    
    # ユーティリティ
    'reset_to_current_track',
    'skip_all_queue',
    'setup_spotify_auth_alias',
    'set_spotify_auth_code_alias',
    'add_song_to_queue',
    'get_current_playing',
    'spotify_pause',
    'spotify_skip',
    'spotify_previous',
    
    # 初期化
    'initialize_spotify',
    
    # 互換性維持
    '_get_spotify',
    '_get_spotify_user',
    'get_spotify_queue',
    'find_and_play_spotify_music',
    'find_and_queue_spotify_music',
    'queue_playlist'
]