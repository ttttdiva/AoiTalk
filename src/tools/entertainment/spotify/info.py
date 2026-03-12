"""
Spotify情報取得モジュール
"""

import spotipy
import logging
from ...core import tool as function_tool

logger = logging.getLogger(__name__)


def get_spotify_status() -> str:
    """Spotifyの現在の再生状態を取得します
    
    Returns:
        現在の再生状態の詳細情報
    """
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    
    print(f"[Tool] get_spotify_status が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify状態取得にはユーザー認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        # 現在の再生状態を取得
        current_playback = spotify.current_playback()
        
        if not current_playback:
            return "Spotifyデバイスがアクティブではありません。Spotifyアプリで音楽を再生してください。"
        
        # 基本的な再生情報
        is_playing = current_playback.get('is_playing', False)
        progress_ms = current_playback.get('progress_ms', 0)
        shuffle_state = current_playback.get('shuffle_state', False)
        repeat_state = current_playback.get('repeat_state', 'off')
        
        # 現在の楽曲情報
        track = current_playback.get('item')
        if track:
            track_name = track['name']
            artists = ", ".join([artist['name'] for artist in track['artists']])
            album_name = track['album']['name']
            duration_ms = track.get('duration_ms', 0)
            
            # 進行状況を計算
            if duration_ms > 0:
                progress_percent = (progress_ms / duration_ms) * 100
                progress_min = progress_ms // 60000
                progress_sec = (progress_ms % 60000) // 1000
                duration_min = duration_ms // 60000
                duration_sec = (duration_ms % 60000) // 1000
            else:
                progress_percent = 0
                progress_min = progress_sec = duration_min = duration_sec = 0
            
            # デバイス情報
            device = current_playback.get('device', {})
            device_name = device.get('name', '不明')
            device_type = device.get('type', '不明')
            volume_percent = device.get('volume_percent', 0)
            
            # 内部キュー情報
            internal_queue = get_internal_queue()
            queue_info = internal_queue.get_queue()
            queue_count = len(queue_info)
            
            # ステータス情報を構築
            status_text = "🎵 Spotify再生状態\n\n"
            status_text += f"📀 楽曲: {track_name}\n"
            status_text += f"👤 アーティスト: {artists}\n"
            status_text += f"💿 アルバム: {album_name}\n"
            status_text += f"⏯️ 状態: {'再生中' if is_playing else '一時停止'}\n"
            status_text += f"⏰ 進行: {progress_min:02d}:{progress_sec:02d} / {duration_min:02d}:{duration_sec:02d} ({progress_percent:.1f}%)\n"
            status_text += f"🔀 シャッフル: {'オン' if shuffle_state else 'オフ'}\n"
            status_text += f"🔁 リピート: {repeat_state}\n"
            status_text += f"📱 デバイス: {device_name} ({device_type})\n"
            status_text += f"🔊 音量: {volume_percent}%\n"
            status_text += f"📋 内部キュー: {queue_count}曲待機中\n"
            
            # 内部キューの次の楽曲を表示
            if queue_count > 0:
                next_track = queue_info[0]
                status_text += f"⏭️ 次の楽曲: {next_track.get('name', '不明')} - {next_track.get('artist', '不明')}\n"
            
            return status_text
        else:
            return "現在再生中の楽曲情報を取得できませんでした。"
            
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"状態取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def get_track_info(track_uri: str) -> str:
    """楽曲の詳細情報を取得します
    
    Args:
        track_uri: 楽曲のSpotify URI
        
    Returns:
        楽曲の詳細情報
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] get_track_info が呼び出されました: uri='{track_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        track_id = track_uri.split(":")[-1]
        track = spotify.track(track_id)
        
        if not track:
            return "楽曲が見つかりませんでした。"
        
        # 楽曲情報を構築
        artists = ", ".join([artist['name'] for artist in track['artists']])
        duration_min = track['duration_ms'] // 60000
        duration_sec = (track['duration_ms'] % 60000) // 1000
        
        # オーディオ特徴量を取得
        try:
            audio_features = spotify.audio_features([track_id])[0]
            if audio_features:
                danceability = int(audio_features['danceability'] * 100)
                energy = int(audio_features['energy'] * 100)
                valence = int(audio_features['valence'] * 100)
                tempo = int(audio_features['tempo'])
            else:
                danceability = energy = valence = tempo = None
        except:
            danceability = energy = valence = tempo = None
        
        info_text = f"🎵 楽曲詳細情報\n\n"
        info_text += f"📀 楽曲: {track['name']}\n"
        info_text += f"👤 アーティスト: {artists}\n"
        info_text += f"💿 アルバム: {track['album']['name']}\n"
        info_text += f"📅 リリース日: {track['album']['release_date']}\n"
        info_text += f"⏰ 再生時間: {duration_min:02d}:{duration_sec:02d}\n"
        info_text += f"🌟 人気度: {track['popularity']}/100\n"
        info_text += f"🔗 URI: {track['uri']}\n"
        
        if danceability is not None:
            info_text += f"\n📊 オーディオ特徴量:\n"
            info_text += f"💃 ダンサビリティ: {danceability}%\n"
            info_text += f"⚡ エネルギー: {energy}%\n"
            info_text += f"😊 ポジティブ度: {valence}%\n"
            info_text += f"🥁 テンポ: {tempo} BPM\n"
        
        return info_text
        
    except spotipy.exceptions.SpotifyException as e:
        return f"楽曲情報取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def get_album_info(album_uri: str) -> str:
    """アルバムの詳細情報を取得します
    
    Args:
        album_uri: アルバムのSpotify URI
        
    Returns:
        アルバムの詳細情報
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] get_album_info が呼び出されました: uri='{album_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        album_id = album_uri.split(":")[-1]
        album = spotify.album(album_id)
        
        if not album:
            return "アルバムが見つかりませんでした。"
        
        # アルバム情報を構築
        artists = ", ".join([artist['name'] for artist in album['artists']])
        
        # 総再生時間を計算
        total_duration_ms = sum(track['duration_ms'] for track in album['tracks']['items'])
        total_duration_min = total_duration_ms // 60000
        total_duration_sec = (total_duration_ms % 60000) // 1000
        
        info_text = f"💿 アルバム詳細情報\n\n"
        info_text += f"💿 アルバム: {album['name']}\n"
        info_text += f"👤 アーティスト: {artists}\n"
        info_text += f"📅 リリース日: {album['release_date']}\n"
        info_text += f"🎵 楽曲数: {album['total_tracks']}曲\n"
        info_text += f"⏰ 総再生時間: {total_duration_min}:{total_duration_sec:02d}\n"
        info_text += f"🌟 人気度: {album['popularity']}/100\n"
        info_text += f"🔗 URI: {album['uri']}\n"
        
        # ジャンル情報があれば追加
        if album.get('genres'):
            info_text += f"🎼 ジャンル: {', '.join(album['genres'])}\n"
        
        # 楽曲リストを追加（最初の10曲）
        info_text += f"\n📋 収録楽曲:\n"
        for i, track in enumerate(album['tracks']['items'][:10], 1):
            track_artists = ", ".join([artist['name'] for artist in track['artists']])
            track_duration_min = track['duration_ms'] // 60000
            track_duration_sec = (track['duration_ms'] % 60000) // 1000
            info_text += f"{i}. {track['name']} - {track_artists} ({track_duration_min}:{track_duration_sec:02d})\n"
        
        if album['total_tracks'] > 10:
            info_text += f"... (残り{album['total_tracks'] - 10}曲)\n"
        
        return info_text
        
    except spotipy.exceptions.SpotifyException as e:
        return f"アルバム情報取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def get_artist_info(artist_uri: str) -> str:
    """アーティストの詳細情報を取得します
    
    Args:
        artist_uri: アーティストのSpotify URI
        
    Returns:
        アーティストの詳細情報
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] get_artist_info が呼び出されました: uri='{artist_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        artist_id = artist_uri.split(":")[-1]
        artist = spotify.artist(artist_id)
        
        if not artist:
            return "アーティストが見つかりませんでした。"
        
        # トップトラックを取得
        top_tracks = spotify.artist_top_tracks(artist_id)
        
        info_text = f"👤 アーティスト詳細情報\n\n"
        info_text += f"👤 アーティスト: {artist['name']}\n"
        info_text += f"👥 フォロワー: {artist['followers']['total']:,}人\n"
        info_text += f"🌟 人気度: {artist['popularity']}/100\n"
        
        # ジャンル情報
        if artist.get('genres'):
            info_text += f"🎼 ジャンル: {', '.join(artist['genres'])}\n"
        
        info_text += f"🔗 URI: {artist['uri']}\n"
        
        # トップトラック（最初の5曲）
        if top_tracks['tracks']:
            info_text += f"\n🔥 人気楽曲:\n"
            for i, track in enumerate(top_tracks['tracks'][:5], 1):
                info_text += f"{i}. {track['name']} ({track['album']['name']})\n"
        
        return info_text
        
    except spotipy.exceptions.SpotifyException as e:
        return f"アーティスト情報取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def get_playlist_info(playlist_uri: str) -> str:
    """プレイリストの詳細情報を取得します
    
    Args:
        playlist_uri: プレイリストのSpotify URI
        
    Returns:
        プレイリストの詳細情報
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] get_playlist_info が呼び出されました: uri='{playlist_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        playlist_id = playlist_uri.split(":")[-1]
        playlist = spotify.playlist(playlist_id)
        
        if not playlist:
            return "プレイリストが見つかりませんでした。"
        
        info_text = f"📂 プレイリスト詳細情報\n\n"
        info_text += f"📂 プレイリスト: {playlist['name']}\n"
        info_text += f"👤 作成者: {playlist['owner']['display_name']}\n"
        info_text += f"🎵 楽曲数: {playlist['tracks']['total']}曲\n"
        info_text += f"👥 フォロワー: {playlist['followers']['total']:,}人\n"
        info_text += f"🌍 公開: {'はい' if playlist['public'] else 'いいえ'}\n"
        
        if playlist.get('description'):
            info_text += f"📝 説明: {playlist['description']}\n"
        
        info_text += f"🔗 URI: {playlist['uri']}\n"
        
        # 楽曲リスト（最初の10曲）
        if playlist['tracks']['items']:
            info_text += f"\n📋 収録楽曲:\n"
            for i, item in enumerate(playlist['tracks']['items'][:10], 1):
                if item['track']:
                    track = item['track']
                    artists = ", ".join([artist['name'] for artist in track['artists']])
                    info_text += f"{i}. {track['name']} - {artists}\n"
        
        if playlist['tracks']['total'] > 10:
            info_text += f"... (残り{playlist['tracks']['total'] - 10}曲)\n"
        
        return info_text
        
    except spotipy.exceptions.SpotifyException as e:
        return f"プレイリスト情報取得エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"