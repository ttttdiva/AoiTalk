"""
Spotify内部キューシステム管理モジュール
"""

import logging
import asyncio
from typing import List, Dict, Optional, Any
from ...core import tool as function_tool

# Import logging functionality
from ..spotify_logger import get_spotify_logger

logger = logging.getLogger(__name__)


class InternalQueue:
    """Spotify APIの制限を回避するための内部キューシステム"""
    
    def __init__(self):
        self.queue = []  # [{'uri': 'spotify:track:xxx', 'name': 'song', 'artist': 'artist'}, ...]
        
    def add(self, track_info: Dict[str, Any]):
        """キューに楽曲を追加"""
        self.queue.append(track_info)
        
    def clear(self):
        """キューをクリア"""
        self.queue.clear()
        
    def get_queue(self) -> List[Dict[str, Any]]:
        """現在のキューを取得"""
        return self.queue.copy()
        
    def remove_at(self, index: int):
        """指定インデックスの楽曲を削除"""
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            
    def get_next(self) -> Optional[Dict[str, Any]]:
        """次の楽曲を取得してキューから削除"""
        if self.queue:
            return self.queue.pop(0)
        return None
    
    def peek_next(self) -> Optional[Dict[str, Any]]:
        """次の楽曲を取得するがキューから削除しない"""
        if self.queue:
            return self.queue[0]
        return None
        
    def has_next(self) -> bool:
        """次の楽曲があるかチェック"""
        return len(self.queue) > 0
    
    def size(self) -> int:
        """キューのサイズを取得"""
        return len(self.queue)


# グローバル内部キュー
_internal_queue = InternalQueue()


def get_internal_queue() -> InternalQueue:
    """内部キューのインスタンスを取得"""
    return _internal_queue


def queue_song(song_name: str, artist_name: str = "") -> str:
    """楽曲をキューに追加（現在の再生は継続）
    
    Args:
        song_name: 楽曲名
        artist_name: アーティスト名（オプション）
        
    Returns:
        結果メッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] queue_song が呼び出されました: song_name='{song_name}', artist_name='{artist_name}'")
    
    try:
        manager = get_spotify_manager()
        if not manager:
            return "Spotify管理クラスが初期化されていません。"
        
        # 検索は認証不要のクライアントを使用
        spotify = manager._get_spotify_client()
        if not spotify:
            return "Spotify検索機能が初期化されていません。設定を確認してください。"
        
        # 楽曲を検索
        tracks = []
        
        # artist_nameが空で「の」が含まれている場合、アーティスト名と曲名に分けて検索
        if not artist_name and 'の' in song_name:
            parts = song_name.split('の', 1)
            if len(parts) == 2:
                artist_part = parts[0].strip()
                track_part = parts[1].strip()
                search_query = f"{artist_part} {track_part}"
                print(f"[queue_song] 「の」検出: アーティスト名と曲名で検索 - '{search_query}'")
                try:
                    search_results = spotify.search(q=search_query, type='track', limit=1)
                    tracks = search_results.get('tracks', {}).get('items', [])
                except Exception:
                    pass
        
        # 上記で見つからない場合、通常の検索
        if not tracks:
            search_query = f"{song_name} {artist_name}".strip()
            try:
                search_results = spotify.search(q=search_query, type='track', limit=1)
                tracks = search_results.get('tracks', {}).get('items', [])
                
                if not tracks:
                    return f"楽曲 '{search_query}' が見つかりませんでした。"
            except Exception as e:
                return f"楽曲検索エラー: {e}"
        
        track = tracks[0]
        track_info = {
            'uri': track['uri'],
            'name': track['name'],
            'artist': ', '.join([artist['name'] for artist in track['artists']])
        }
        
        # 内部キューに追加
        _internal_queue.add(track_info)
        queue_position = len(_internal_queue.queue)
        
        # Spotifyのキューにも追加（内部キューと同期）
        user_spotify = manager._get_spotify_user_client()
        if user_spotify:
            try:
                    user_spotify.add_to_queue(track['uri'])
                    result_message = f"🎵 「{track_info['name']} - {track_info['artist']}」をキューの{queue_position}番目に追加しました。現在の楽曲は継続されます。"
            except Exception as e:
                # Spotifyキューへの追加が失敗しても内部キューには追加済み
                result_message = f"🎵 「{track_info['name']} - {track_info['artist']}」を内部キューの{queue_position}番目に追加しました（Spotifyキューへの追加は失敗）。"
        else:
            result_message = f"🎵 「{track_info['name']} - {track_info['artist']}」を内部キューの{queue_position}番目に追加しました。"
        
        # Log the activity
        try:
                logger_instance = get_spotify_logger()
                asyncio.create_task(logger_instance.log_activity(
                    user_id="default_user",  # TODO: Get from context
                    character_name="assistant",  # TODO: Get from context
                    action="queue",
                    track_info=track,
                    request_text=f"queue_song({song_name}, {artist_name})",
                    success=True,
                    queue_position=queue_position
                ))
        except Exception as log_error:
            print(f"[SpotifyLogger] Error logging queue_song: {log_error}")
        
        return result_message
        
    except Exception as e:
        logger.error(f"キュー追加エラー: {e}")
        return f"キューへの追加に失敗しました: {e}"


def show_queue() -> str:
    """現在のキューを表示（キューの内容を確認するだけの読み取り専用操作）
    
    注意：この関数は表示のみで、キューの状態を変更しません。
    自動キューの停止など他の操作は行いません。
    
    Returns:
        キューの内容
    """
    try:
        queue = _internal_queue.get_queue()
        
        if not queue:
            return "キューは空です。"
        
        result = f"現在のキュー（{len(queue)}曲）:\n"
        for i, track in enumerate(queue, 1):
            name = track.get('name', '不明')
            artist = track.get('artist', '不明')
            result += f"{i}. {name}"
            if artist and artist != '不明':
                result += f" - {artist}"
            result += "\n"
        
        return result.strip()
        
    except Exception as e:
        logger.error(f"キュー表示エラー: {e}")
        return f"キューの表示に失敗しました: {e}"


def clear_spotify_queue() -> str:
    """内部キューをクリア（現在の楽曲は継続）
    
    Returns:
        結果メッセージ
    """
    try:
        queue_size = len(_internal_queue.queue)
        _internal_queue.clear()
        
        if queue_size > 0:
            result_message = f"キューをクリアしました（{queue_size}曲を削除）。現在の楽曲は継続されます。"
        else:
            result_message = "キューは既に空でした。"
        
        # Log the activity
        try:
            logger_instance = get_spotify_logger()
            asyncio.create_task(logger_instance.log_activity(
                user_id="default_user",  # TODO: Get from context
                character_name="assistant",  # TODO: Get from context
                action="clear_queue",
                request_text="clear_spotify_queue()",
                success=True,
                queue_size_cleared=queue_size
            ))
        except Exception as log_error:
            print(f"[SpotifyLogger] Error logging clear_spotify_queue: {log_error}")
        
        return result_message
            
    except Exception as e:
        logger.error(f"キュークリアエラー: {e}")
        return f"キューのクリアに失敗しました: {e}"


def remove_from_queue(position: int) -> str:
    """キューから指定位置の楽曲を削除
    
    Args:
        position: 削除する楽曲の位置（1から開始）
        
    Returns:
        結果メッセージ
    """
    try:
        queue = _internal_queue.get_queue()
        
        if not queue:
            return "キューは空です。"
        
        if position < 1 or position > len(queue):
            return f"無効な位置です。1から{len(queue)}の間で指定してください。"
        
        # 0ベースのインデックスに変換
        index = position - 1
        track = queue[index]
        track_name = track.get('name', '不明')
        
        _internal_queue.remove_at(index)
        
        return f"キューの{position}番目「{track_name}」を削除しました。"
        
    except Exception as e:
        logger.error(f"キューからの削除エラー: {e}")
        return f"キューからの削除に失敗しました: {e}"