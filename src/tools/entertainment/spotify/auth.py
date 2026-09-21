"""
Spotify認証管理モジュール
"""

import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyClientCredentials
import spotipy.cache_handler
import logging
from typing import Any, Optional
import os
from collections.abc import Mapping

from ...core import tool
from ....services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)

logger = logging.getLogger(__name__)


class SpotifyManager:
    """Spotify API管理クラス"""
    
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, config=None):
        """Spotify管理クラスの初期化
        
        Args:
            client_id: Spotify Client ID
            client_secret: Spotify Client Secret  
            redirect_uri: リダイレクトURI
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.config = config
        self._spotify = None

    def _privacy_gateway(self) -> OutboundPrivacyGateway:
        """Resolve the request-scoped gateway used by every Spotipy call."""

        context = get_privacy_policy_context()
        config = self.config
        if config is None:
            try:
                from ....config import Config

                config = Config()
            except Exception as exc:
                # Entertainment integrations are optional, but once enabled
                # an unavailable privacy policy must fail closed rather than
                # silently restoring Spotipy's direct transport.
                raise PrivacyError("Spotify privacy configuration is unavailable") from exc
        session = context.session_context or {}
        return OutboundPrivacyGateway(
            config,
            user_id=str(session.get("user_id") or ""),
            session_id=str(session.get("session_id") or session.get("id") or ""),
            session_context=context.session_context,
            project_metadata=context.project_metadata,
        )

    def _wrap_spotify_client(self, client):
        """Route Spotipy's sole HTTP primitive through the common boundary.

        All public Spotipy helpers eventually call ``_internal_call``.  A
        narrow wrapper there avoids a fragile one-off wrapper for every
        playlist/playback/search method while still binding method, path and
        provider before the SDK can emit a request.  SDK retries are disabled;
        a retry is a fresh gateway transaction at the caller's layer.
        """

        if client is None or getattr(client, "_aoitalk_privacy_wrapped", False):
            return client
        original = getattr(client, "_internal_call", None)
        if not callable(original):
            raise PrivacyError("Spotify client transport is unsupported")
        gateway = self._privacy_gateway()
        prefix = str(getattr(client, "prefix", "https://api.spotify.com/v1") or "").rstrip("/")

        def gated_internal_call(method, url, payload, params):
            method_text = str(method or "GET").upper()
            path_text = str(url or "")
            full_url = path_text if path_text.startswith("http") else f"{prefix}/{path_text.lstrip('/')}"
            envelope = {
                "method": method_text,
                "url": path_text,
                "payload": payload,
                "params": params if isinstance(params, Mapping) else {},
            }

            def sender(final_payload):
                if not isinstance(final_payload, Mapping):
                    raise PrivacyError("Spotify outbound payload is malformed")
                if str(final_payload.get("method") or "").upper() != method_text:
                    raise PrivacyError("Spotify method binding changed")
                if str(final_payload.get("url") or "") != path_text:
                    raise PrivacyError("Spotify destination binding changed")
                final_params = final_payload.get("params")
                if final_params is None:
                    final_params = {}
                if not isinstance(final_params, Mapping):
                    raise PrivacyError("Spotify query parameters are malformed")
                return original(
                    method_text,
                    path_text,
                    final_payload.get("payload"),
                    dict(final_params),
                )

            return gateway.execute_sync(
                envelope,
                provider="spotify",
                descriptor=EgressDescriptor(
                    action=f"spotify.{method_text.lower()}",
                    transport="spotipy._internal_call",
                    destination=full_url,
                    provider="spotify",
                    tool="spotify",
                ),
                sender=sender,
                base_url=full_url,
                source_kind="spotify_api",
            )

        client._internal_call = gated_internal_call
        client._aoitalk_privacy_wrapped = True
        # A boundary transaction owns retry semantics.  Spotipy's adapter
        # retries are otherwise an invisible second send after approval.
        try:
            client.retries = 0
        except Exception:
            pass
        return client
        
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
                
                self._spotify = self._wrap_spotify_client(spotipy.Spotify(
                    auth_manager=auth_manager,
                    requests_timeout=10,  # タイムアウトを10秒に設定
                    retries=0,
                ))
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
            
            return self._wrap_spotify_client(spotipy.Spotify(
                auth_manager=auth_manager,
                requests_timeout=10,  # タイムアウトを10秒に設定
                retries=0,
            ))
            
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
        
        # 認証コードからトークンを取得。OAuth の token exchange は
        # Spotipy の ``_internal_call`` を通らないため、ここも同じ
        # transaction boundary で包む。
        def send_oauth_payload(final_payload: Any):
            if not isinstance(final_payload, Mapping):
                raise PrivacyError("Spotify OAuth payload is malformed")
            final_auth_code = str(final_payload.get("auth_code") or "")
            if not final_auth_code:
                raise PrivacyError("Spotify OAuth code is empty")
            return auth_manager.get_access_token(final_auth_code)

        token_info = manager._privacy_gateway().execute_sync(
            {"auth_code": str(auth_code or "")},
            provider="spotify",
            descriptor=EgressDescriptor(
                action="spotify.oauth_token",
                transport="requests.post",
                destination="https://accounts.spotify.com/api/token",
                provider="spotify",
                tool="spotify.auth",
            ),
            base_url="https://accounts.spotify.com/api/token",
            source_kind="spotify_oauth",
            sender=send_oauth_payload,
        )
        
        if token_info:
            return "Spotify認証が完了しました！音楽の再生・制御が可能になりました。"
        else:
            return "認証に失敗しました。正しい認証コードを入力してください。"
            
    except Exception as e:
        logger.error(f"認証コード設定エラー: {e}")
        return f"認証に失敗しました: {e}"
