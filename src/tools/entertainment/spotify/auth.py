"""
Spotify認証管理モジュール
"""

import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyClientCredentials
import spotipy.cache_handler
import logging
from typing import Optional
import os

from ...core import tool

logger = logging.getLogger(__name__)


class SpotifyManager:
    """Spotify API管理クラス"""
    
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        """Spotify管理クラスの初期化
        
        Args:
            client_id: Spotify Client ID
            client_secret: Spotify Client Secret  
            redirect_uri: リダイレクトURI
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._spotify = None
        
    def _get_spotify_client(self) -> Optional[spotipy.Spotify]:
        """Spotifyクライアントを取得（Client Credentials認証）"""
        if self._spotify is None:
            try:
                # キャッシュディレクトリを作成
                cache_dir = ".cache"
                if not os.path.exists(cache_dir):
                    os.makedirs(cache_dir)
                
                # Client Credentials認証（検索専用、ユーザー認証不要）
                auth_manager = SpotifyClientCredentials(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    cache_handler=spotipy.cache_handler.CacheFileHandler(cache_path=".cache/spotify_client_cache")
                )
                
                self._spotify = spotipy.Spotify(
                    auth_manager=auth_manager,
                    requests_timeout=10,  # タイムアウトを10秒に設定
                    retries=3  # リトライ回数を3回に設定
                )
                logger.info("Spotifyクライアントを初期化しました（検索専用モード）")
                
            except Exception as e:
                logger.error(f"Spotify認証エラー: {e}")
                return None
                
        return self._spotify
        
    def _get_spotify_user_client(self) -> Optional[spotipy.Spotify]:
        """ユーザー認証が必要な機能用のSpotifyクライアントを取得"""
        try:
            # キャッシュディレクトリを作成
            cache_dir = ".cache"
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
            
            scope = "user-read-playback-state,user-modify-playback-state,user-read-currently-playing,playlist-read-private,playlist-read-collaborative,user-library-read,playlist-modify-public,playlist-modify-private"
            
            auth_manager = SpotifyOAuth(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri=self.redirect_uri,
                scope=scope,
                cache_handler=spotipy.cache_handler.CacheFileHandler(cache_path=".cache/spotify_cache"),
                open_browser=False
            )
            
            return spotipy.Spotify(
                auth_manager=auth_manager,
                requests_timeout=10,  # タイムアウトを10秒に設定
                retries=3  # リトライ回数を3回に設定
            )
            
        except Exception as e:
            logger.error(f"Spotifyユーザー認証エラー: {e}")
            return None


# グローバルインスタンス
_spotify_manager: Optional[SpotifyManager] = None


def init_spotify_manager():
    """Spotifyマネージャーを初期化"""
    global _spotify_manager
    
    # 環境変数から認証情報を取得
    client_id = os.getenv('SPOTIFY_CLIENT_ID')
    client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
    redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:8080/callback')
    
    if not client_id or not client_secret:
        logger.error("Spotify認証情報が設定されていません")
        return False
    
    _spotify_manager = SpotifyManager(client_id, client_secret, redirect_uri)
    logger.info("Spotifyマネージャーを初期化しました")
    return True


def get_spotify_manager() -> Optional[SpotifyManager]:
    """Spotifyマネージャーを取得"""
    global _spotify_manager
    if _spotify_manager is None:
        init_spotify_manager()
    return _spotify_manager


def _get_spotify() -> Optional[spotipy.Spotify]:
    """Spotifyクライアントを取得（検索専用）"""
    manager = get_spotify_manager()
    if manager:
        return manager._get_spotify_client()
    return None


def _get_spotify_user() -> Optional[spotipy.Spotify]:
    """ユーザー認証Spotifyクライアントを取得"""
    manager = get_spotify_manager()
    if manager:
        return manager._get_spotify_user_client()
    return None


@tool
def setup_spotify_auth():
    """Spotify認証をセットアップ"""
    manager = get_spotify_manager()
    if not manager:
        return "Spotify認証情報が設定されていません"
    
    try:
        # 認証URLを生成
        auth_manager = SpotifyOAuth(
            client_id=manager.client_id,
            client_secret=manager.client_secret,
            redirect_uri=manager.redirect_uri,
            scope="user-read-playback-state,user-modify-playback-state,user-read-currently-playing,playlist-read-private,playlist-read-collaborative,user-library-read,playlist-modify-public,playlist-modify-private",
            cache_handler=spotipy.cache_handler.CacheFileHandler(cache_path=".cache/spotify_cache"),
            open_browser=False
        )
        
        auth_url = auth_manager.get_authorize_url()
        return f"以下のURLにアクセスして認証してください:\n{auth_url}\n\n認証後、リダイレクトされたURLの'code'パラメータの値を教えてください。"
        
    except Exception as e:
        logger.error(f"認証セットアップエラー: {e}")
        return f"認証セットアップに失敗しました: {e}"


@tool
def set_spotify_auth_code(auth_code: str):
    """認証コードを設定"""
    manager = get_spotify_manager()
    if not manager:
        return "Spotify認証情報が設定されていません"
    
    try:
        auth_manager = SpotifyOAuth(
            client_id=manager.client_id,
            client_secret=manager.client_secret,
            redirect_uri=manager.redirect_uri,
            scope="user-read-playback-state,user-modify-playback-state,user-read-currently-playing,playlist-read-private,playlist-read-collaborative,user-library-read,playlist-modify-public,playlist-modify-private",
            cache_handler=spotipy.cache_handler.CacheFileHandler(cache_path=".cache/spotify_cache"),
            open_browser=False
        )
        
        # 認証コードからトークンを取得
        token_info = auth_manager.get_access_token(auth_code)
        
        if token_info:
            return "Spotify認証が完了しました！音楽の再生・制御が可能になりました。"
        else:
            return "認証に失敗しました。正しい認証コードを入力してください。"
            
    except Exception as e:
        logger.error(f"認証コード設定エラー: {e}")
        return f"認証に失敗しました: {e}"