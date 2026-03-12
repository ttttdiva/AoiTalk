"""
Spotify検索機能モジュール
"""

import spotipy
import logging
from ...core import tool as function_tool

logger = logging.getLogger(__name__)


def search_spotify_music(query: str, search_type: str = "track", limit: int = 5) -> str:
    """Spotifyで音楽を検索します
    
    Args:
        query: 検索クエリ（楽曲名、アーティスト名、アルバム名など）
        search_type: 検索タイプ（"track", "artist", "album", "playlist"）
        limit: 検索結果の最大数（デフォルト5）
        
    Returns:
        検索結果の詳細情報
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] search_spotify_music が呼び出されました: query='{query}', type='{search_type}', limit={limit}")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    # 検索は認証不要のクライアントを使用
    spotify = manager._get_spotify_client()
    if not spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        # まず元のクエリで検索を試行
        try:
            results = spotify.search(q=query, type=search_type, limit=limit)
            
            # 結果が見つかった場合はそのまま返す
            if results[f'{search_type}s']['items']:
                pass  # そのまま処理を続ける
            else:
                # 結果が見つからなかった場合のみ、追加の検索パターンを試行
                search_queries = []
                
                # アーティスト検索の場合、追加の検索パターンを使用
                if search_type == "artist":
                    search_queries = [
                        f'artist:{query}',  # アーティスト特定検索
                        query.lower(),  # 小文字変換
                    ]
                # プレイリスト検索の場合、追加の検索パターンを使用
                elif search_type == "playlist":
                    search_queries = [
                        f'{query} playlist',  # プレイリストという単語を追加
                    ]
                # トラック検索の場合
                else:
                    search_queries = [
                        f'"{query}"',  # 完全一致検索
                        query.lower(),  # 小文字変換
                    ]
                
                # 追加パターンで検索
                for search_query in search_queries:
                    try:
                        result = spotify.search(q=search_query, type=search_type, limit=limit)
                        if result[f'{search_type}s']['items']:
                            results = result
                            break
                    except Exception:
                        continue
                        
        except Exception as e:
            # 最初の検索でエラーが発生した場合
            return f"Spotify検索エラー: {str(e)}"
        
        if search_type == "track":
            tracks = results['tracks']['items']
            # nullを除外して有効なトラックのみ取得
            valid_tracks = [t for t in tracks if t is not None]
            
            if not valid_tracks:
                return f"'{query}'に該当する楽曲が見つかりませんでした。"
                
            output = f"🎵 検索結果: '{query}'\n\n"
            for i, track in enumerate(valid_tracks, 1):
                artists = ", ".join([artist['name'] for artist in track['artists']])
                output += f"{i}. {track['name']} - {artists}\n"
                output += f"   アルバム: {track['album']['name']}\n"
                output += f"   URI: {track['uri']}\n\n"
                
            return output
            
        elif search_type == "artist":
            artists = results['artists']['items']
            # nullを除外して有効なアーティストのみ取得
            valid_artists = [a for a in artists if a is not None]
            
            if not valid_artists:
                return f"'{query}'に該当するアーティストが見つかりませんでした。"
                
            output = f"👤 アーティスト検索結果: '{query}'\n\n"
            for i, artist in enumerate(valid_artists, 1):
                output += f"{i}. {artist['name']}\n"
                output += f"   フォロワー: {artist['followers']['total']:,}\n"
                output += f"   URI: {artist['uri']}\n\n"
                
            return output
            
        elif search_type == "album":
            albums = results['albums']['items']
            # nullを除外して有効なアルバムのみ取得
            valid_albums = [a for a in albums if a is not None]
            
            if not valid_albums:
                return f"'{query}'に該当するアルバムが見つかりませんでした。"
                
            output = f"💿 アルバム検索結果: '{query}'\n\n"
            for i, album in enumerate(valid_albums, 1):
                artists = ", ".join([artist['name'] for artist in album['artists']])
                output += f"{i}. {album['name']} - {artists}\n"
                output += f"   リリース日: {album['release_date']}\n"
                output += f"   楽曲数: {album['total_tracks']}\n"
                output += f"   URI: {album['uri']}\n\n"
                
            return output
            
        elif search_type == "playlist":
            playlists = results['playlists']['items']
            # nullを除外して有効なプレイリストのみ取得
            valid_playlists = [p for p in playlists if p is not None]
            
            if not valid_playlists:
                # プレイリスト検索でアーティスト名を含む特別検索を試行
                try:
                    artist_search = spotify.search(q=f'artist:{query}', type='playlist', limit=limit)
                    valid_playlists = [p for p in artist_search['playlists']['items'] if p is not None]
                except Exception:
                    pass
                    
                if not valid_playlists:
                    return f"'{query}'に該当するプレイリストが見つかりませんでした。"
                
            output = f"📂 プレイリスト検索結果: '{query}'\n\n"
            for i, playlist in enumerate(valid_playlists, 1):
                output += f"{i}. {playlist['name']}\n"
                output += f"   作成者: {playlist['owner']['display_name']}\n"
                output += f"   楽曲数: {playlist['tracks']['total']}\n"
                output += f"   URI: {playlist['uri']}\n\n"
                
            return output
        
    except spotipy.exceptions.SpotifyException as e:
        return f"Spotify検索エラー: {str(e)}"
    except Exception as e:
        return f"検索エラー: {str(e)}"


def _validate_track(spotify_uri: str) -> bool:
    """楽曲が存在するかを検証
    
    Args:
        spotify_uri: 検証するSpotify URI
        
    Returns:
        楽曲が存在する場合True
    """
    from .auth import get_spotify_manager
    
    try:
        manager = get_spotify_manager()
        if not manager:
            return False
        
        spotify = manager._get_spotify_client()
        if not spotify:
            return False
        
        track_id = spotify_uri.split(":")[-1]
        track = spotify.track(track_id)
        return track is not None
        
    except Exception as e:
        logger.error(f"楽曲検証エラー: {e}")
        return False