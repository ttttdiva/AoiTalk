"""
Spotifyユーティリティ機能モジュール
"""

import spotipy
import logging
from ...core import tool as function_tool

logger = logging.getLogger(__name__)


def reset_to_current_track() -> str:
    """現在の楽曲にリセットし、監視システムを再同期します
    
    Returns:
        リセット結果のメッセージ
    """
    from .auth import get_spotify_manager
    from .monitoring import update_last_track_id, request_monitor_reset
    
    print(f"[Tool] reset_to_current_track が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生制御を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        # 現在の再生状態を取得
        current_playback = spotify.current_playback()
        
        if not current_playback:
            return "Spotifyデバイスがアクティブではありません。"
        
        current_track = current_playback.get('item')
        if not current_track:
            return "現在再生中の楽曲が見つかりません。"
        
        # 監視システムを現在の楽曲にリセット
        current_track_id = current_track['id']
        update_last_track_id(current_track_id)
        request_monitor_reset()
        
        track_name = current_track['name']
        artists = ", ".join([artist['name'] for artist in current_track['artists']])
        
        return f"🔄 監視システムを現在の楽曲にリセットしました:\n楽曲: {track_name}\nアーティスト: {artists}"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 404:
            return "アクティブなSpotifyデバイスが見つかりません。"
        elif e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"リセットエラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def skip_all_queue() -> str:
    """キューの全楽曲をスキップします（緊急用）
    
    Returns:
        スキップ結果のメッセージ
    """
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    from .monitoring import set_skip_in_progress
    
    print(f"[Tool] skip_all_queue が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生制御を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    internal_queue = get_internal_queue()
    queue_count = len(internal_queue.get_queue())
    
    if queue_count == 0:
        return "内部キューは既に空です。"
    
    try:
        # スキップ処理中フラグを設定
        set_skip_in_progress(True)
        
        # 内部キューをクリア
        internal_queue.clear()
        
        # 現在のSpotifyキューもスキップを試行
        try:
            # 複数回のスキップを試行（Spotifyキューの楽曲もスキップ）
            for _ in range(min(10, queue_count + 5)):  # 最大10回まで
                try:
                    current_playback = spotify.current_playback()
                    if not current_playback or not current_playback.get('is_playing'):
                        break
                    
                    spotify.next_track()
                    import time
                    time.sleep(0.2)  # 短い待機
                except spotipy.exceptions.SpotifyException as e:
                    if e.http_status == 404:
                        break  # デバイスが見つからない場合は停止
                    else:
                        continue
                except:
                    continue
        except Exception as e:
            logger.warning(f"Spotifyキューのスキップ中にエラー: {e}")
        
        return f"⏭️ 全キューをスキップしました（内部キュー: {queue_count}曲）。再生は現在のSpotifyキューまたは停止状態になります。"
        
    except Exception as e:
        return f"全スキップエラー: {str(e)}"
    finally:
        # スキップ処理完了
        set_skip_in_progress(False)


def setup_spotify_auth_alias() -> str:
    """Spotify認証設定のエイリアス（互換性維持）
    
    Returns:
        認証設定の結果
    """
    from .auth import setup_spotify_auth
    return setup_spotify_auth()


def set_spotify_auth_code_alias(auth_code: str) -> str:
    """Spotify認証コード設定のエイリアス（互換性維持）
    
    Args:
        auth_code: 認証コード
        
    Returns:
        認証結果
    """
    from .auth import set_spotify_auth_code
    return set_spotify_auth_code(auth_code)


# 互換性のための関数エイリアス
def add_song_to_queue(song_name: str, artist_name: str = "") -> str:
    """楽曲をキューに追加（互換性維持）"""
    from .queue_system import queue_song
    return queue_song(song_name, artist_name)


def get_current_playing() -> str:
    """現在の再生状態を取得（互換性維持）"""
    from .info import get_spotify_status
    return get_spotify_status()


def spotify_pause() -> str:
    """再生一時停止（互換性維持）"""
    from .playback_control import pause_spotify
    return pause_spotify()


def spotify_skip() -> str:
    """スキップ（互換性維持）"""
    from .playback_control import skip_spotify_track
    return skip_spotify_track()


def spotify_previous() -> str:
    """前の曲（互換性維持）"""
    from .playback_control import previous_track
    return previous_track()


# 関数のエクスポート（互換性維持用）
__all__ = [
    'reset_to_current_track',
    'skip_all_queue',
    'setup_spotify_auth_alias',
    'set_spotify_auth_code_alias',
    'add_song_to_queue',
    'get_current_playing',
    'spotify_pause',
    'spotify_skip',
    'spotify_previous'
]