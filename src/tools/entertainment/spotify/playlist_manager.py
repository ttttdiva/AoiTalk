"""
Spotifyプレイリスト管理モジュール
"""

import spotipy
import logging
import asyncio
from ...core import tool as function_tool
from typing import List, Dict, Any

# Import logging functionality
from ..spotify_logger import get_spotify_logger

logger = logging.getLogger(__name__)


def get_spotify_user_playlists(limit: int = 20) -> str:
    """ユーザーのプレイリスト一覧を取得します
    
    Args:
        limit: 取得するプレイリストの最大数（デフォルト20）
        
    Returns:
        プレイリスト一覧
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] get_spotify_user_playlists が呼び出されました: limit={limit}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "プレイリスト取得にはSpotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        playlists = spotify.current_user_playlists(limit=limit)
        
        if not playlists['items']:
            return "プレイリストが見つかりませんでした。"
        
        output = f"📂 あなたのプレイリスト ({len(playlists['items'])}個):\n\n"
        for i, playlist in enumerate(playlists['items'], 1):
            output += f"{i}. {playlist['name']}\n"
            output += f"   楽曲数: {playlist['tracks']['total']}\n"
            output += f"   公開: {'はい' if playlist['public'] else 'いいえ'}\n"
            output += f"   URI: {playlist['uri']}\n\n"
        
        return output
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"プレイリスト取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def create_playlist(name: str, description: str = "", public: bool = False) -> str:
    """新しいプレイリストを作成します
    
    Args:
        name: プレイリスト名
        description: プレイリストの説明（オプション）
        public: 公開設定（デフォルトは非公開）
        
    Returns:
        作成結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] create_playlist が呼び出されました: name='{name}', public={public}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "プレイリスト作成にはSpotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        # 現在のユーザー情報を取得
        user = spotify.current_user()
        user_id = user['id']
        
        # プレイリストを作成
        playlist = spotify.user_playlist_create(
            user=user_id,
            name=name,
            public=public,
            description=description
        )
        
        result_message = f"📂 プレイリストを作成しました:\n名前: {playlist['name']}\n説明: {description}\n公開: {'はい' if public else 'いいえ'}\nURI: {playlist['uri']}"
        
        # Log the activity
        try:
            logger_instance = get_spotify_logger()
            asyncio.create_task(logger_instance.log_activity(
                user_id="default_user",  # TODO: Get from context
                character_name="assistant",  # TODO: Get from context
                action="create_playlist",
                playlist_id=playlist['id'],
                playlist_name=playlist['name'],
                request_text=f"create_playlist({name}, {description}, {public})",
                success=True
            ))
        except Exception as log_error:
            print(f"[SpotifyLogger] Error logging create_playlist: {log_error}")
        
        return result_message
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"プレイリスト作成エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def create_playlist_from_queue() -> str:
    """現在の内部キューからプレイリストを作成します
    
    Returns:
        作成結果のメッセージ
    """
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    import datetime
    
    print(f"[Tool] create_playlist_from_queue が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "プレイリスト作成にはSpotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    internal_queue = get_internal_queue()
    queue = internal_queue.get_queue()
    
    if not queue:
        return "キューが空のため、プレイリストを作成できません。"
    
    try:
        # 現在の日時でプレイリスト名を生成
        now = datetime.datetime.now()
        playlist_name = f"キューから作成 - {now.strftime('%Y年%m月%d日 %H:%M')}"
        
        # プレイリストを作成
        user = spotify.current_user()
        user_id = user['id']
        
        playlist = spotify.user_playlist_create(
            user=user_id,
            name=playlist_name,
            public=False,
            description=f"内部キューから自動作成されたプレイリスト ({len(queue)}曲)"
        )
        
        # 楽曲URIを抽出
        track_uris = [track['uri'] for track in queue if track.get('uri')]
        
        if track_uris:
            # プレイリストに楽曲を追加（100曲ずつ）
            for i in range(0, len(track_uris), 100):
                batch = track_uris[i:i+100]
                spotify.playlist_add_items(playlist['id'], batch)
        
        result_message = f"📂 キューからプレイリストを作成しました:\n名前: {playlist_name}\n楽曲数: {len(track_uris)}\nURI: {playlist['uri']}"
        
        # Log the activity
        try:
            logger_instance = get_spotify_logger()
            asyncio.create_task(logger_instance.log_activity(
                user_id="default_user",  # TODO: Get from context
                character_name="assistant",  # TODO: Get from context
                action="create_playlist_from_queue",
                playlist_id=playlist['id'],
                playlist_name=playlist_name,
                request_text="create_playlist_from_queue()",
                success=True,
                tracks_added=len(track_uris)
            ))
        except Exception as log_error:
            print(f"[SpotifyLogger] Error logging create_playlist_from_queue: {log_error}")
        
        return result_message
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"プレイリスト作成エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def add_tracks_to_playlist(playlist_uri: str, track_uris: List[str]) -> str:
    """プレイリストに楽曲を追加します
    
    Args:
        playlist_uri: プレイリストのSpotify URI
        track_uris: 追加する楽曲のSpotify URIのリスト
        
    Returns:
        追加結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] add_tracks_to_playlist が呼び出されました: playlist='{playlist_uri}', tracks={len(track_uris)}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "プレイリスト編集にはSpotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        playlist_id = playlist_uri.split(":")[-1]
        
        # プレイリスト情報を取得
        playlist = spotify.playlist(playlist_id, fields="name")
        
        # 楽曲を100曲ずつ追加
        added_count = 0
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i+100]
            spotify.playlist_add_items(playlist_id, batch)
            added_count += len(batch)
        
        return f"➕ プレイリスト「{playlist['name']}」に{added_count}曲を追加しました。"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        elif e.http_status == 403:
            return "このプレイリストを編集する権限がありません。"
        else:
            return f"楽曲追加エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def add_queue_to_playlist(playlist_uri: str) -> str:
    """現在の内部キューをプレイリストに追加します
    
    Args:
        playlist_uri: 追加先プレイリストのSpotify URI
        
    Returns:
        追加結果のメッセージ
    """
    from .queue_system import get_internal_queue
    
    print(f"[Tool] add_queue_to_playlist が呼び出されました: playlist='{playlist_uri}'")
    
    internal_queue = get_internal_queue()
    queue = internal_queue.get_queue()
    
    if not queue:
        return "キューが空のため、プレイリストに追加する楽曲がありません。"
    
    # 楽曲URIを抽出
    track_uris = [track['uri'] for track in queue if track.get('uri')]
    
    if not track_uris:
        return "キューに有効な楽曲URIがありません。"
    
    return add_tracks_to_playlist(playlist_uri, track_uris)


def remove_tracks_from_playlist(playlist_uri: str, track_uris: List[str]) -> str:
    """プレイリストから楽曲を削除します
    
    Args:
        playlist_uri: プレイリストのSpotify URI
        track_uris: 削除する楽曲のSpotify URIのリスト
        
    Returns:
        削除結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] remove_tracks_from_playlist が呼び出されました: playlist='{playlist_uri}', tracks={len(track_uris)}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "プレイリスト編集にはSpotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        playlist_id = playlist_uri.split(":")[-1]
        
        # プレイリスト情報を取得
        playlist = spotify.playlist(playlist_id, fields="name")
        
        # 楽曲を100曲ずつ削除
        removed_count = 0
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i+100]
            spotify.playlist_remove_all_occurrences_of_items(playlist_id, batch)
            removed_count += len(batch)
        
        return f"➖ プレイリスト「{playlist['name']}」から{removed_count}曲を削除しました。"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        elif e.http_status == 403:
            return "このプレイリストを編集する権限がありません。"
        else:
            return f"楽曲削除エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def add_playlist_to_queue(playlist_uri: str, shuffle: bool = False) -> str:
    """プレイリストの楽曲を内部キューに追加します
    
    Args:
        playlist_uri: プレイリストのSpotify URI
        shuffle: シャッフルするかどうか（デフォルト False）
        
    Returns:
        追加結果のメッセージ
    """
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    
    print(f"[Tool] add_playlist_to_queue が呼び出されました: playlist='{playlist_uri}', shuffle={shuffle}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        playlist_id = playlist_uri.split(":")[-1]
        
        # プレイリスト情報を取得
        playlist = spotify.playlist(playlist_id, fields="name,tracks")
        
        # プレイリストの全楽曲を取得
        tracks = []
        results = spotify.playlist_tracks(playlist_id)
        tracks.extend(results['items'])
        
        while results['next']:
            results = spotify.next(results)
            tracks.extend(results['items'])
        
        # 有効な楽曲のみを抽出
        valid_tracks = []
        for item in tracks:
            if item['track'] and item['track']['uri']:
                track_info = {
                    'uri': item['track']['uri'],
                    'name': item['track']['name'],
                    'artist': ", ".join([artist['name'] for artist in item['track']['artists']])
                }
                valid_tracks.append(track_info)
        
        if not valid_tracks:
            return f"プレイリスト「{playlist['name']}」に有効な楽曲がありません。"
        
        # シャッフルが要求された場合
        if shuffle:
            import random
            random.shuffle(valid_tracks)
        
        # 内部キューに追加
        internal_queue = get_internal_queue()
        for track in valid_tracks:
            internal_queue.add(track)
        
        result_message = f"📂 プレイリスト「{playlist['name']}」から{len(valid_tracks)}曲をキューに追加しました。{'（シャッフル済み）' if shuffle else ''}"
        
        # Log the activity
        try:
            logger_instance = get_spotify_logger()
            asyncio.create_task(logger_instance.log_activity(
                user_id="default_user",  # TODO: Get from context
                character_name="assistant",  # TODO: Get from context
                action="add_playlist_to_queue",
                playlist_id=playlist_id,
                playlist_name=playlist['name'],
                request_text=f"add_playlist_to_queue({playlist_uri}, {shuffle})",
                success=True,
                tracks_added=len(valid_tracks),
                shuffle_enabled=shuffle
            ))
        except Exception as log_error:
            print(f"[SpotifyLogger] Error logging add_playlist_to_queue: {log_error}")
        
        return result_message
        
    except spotipy.exceptions.SpotifyException as e:
        return f"プレイリスト取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"