"""
Spotify API Client with Error Handling
エラーハンドリング付きのSpotify APIクライアント
"""

import logging
import time
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)


class SpotifyAPIClient:
    """エラーハンドリング統一されたSpotify APIクライアント"""
    
    def __init__(self):
        self.consecutive_timeouts = 0
        self.last_success_time = time.time()
    
    def call_with_retry(self, api_func: Callable, operation_name: str, max_retries: int = 3) -> Optional[Any]:
        """
        リトライ機能付きでSpotify APIを呼び出し
        
        Args:
            api_func: 呼び出すAPI関数
            operation_name: 操作名（ログ用）
            max_retries: 最大リトライ回数
            
        Returns:
            API呼び出し結果、または None（失敗時）
        """
        for attempt in range(max_retries + 1):
            try:
                result = api_func()
                
                # 成功時の記録
                self.consecutive_timeouts = 0
                self.last_success_time = time.time()
                
                if attempt > 0:
                    logger.info(f"[APIClient] {operation_name} succeeded after {attempt} retries")
                
                return result
                
            except Exception as e:
                error_type = self._classify_error(e)
                
                if attempt < max_retries:
                    wait_time = self._calculate_wait_time(error_type, attempt)
                    logger.warning(f"[APIClient] {operation_name} failed (attempt {attempt + 1}/{max_retries + 1}): {e}")
                    logger.info(f"[APIClient] Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"[APIClient] {operation_name} failed after {max_retries + 1} attempts: {e}")
                    
                    if error_type == 'timeout':
                        self.consecutive_timeouts += 1
        
        return None
    
    def _classify_error(self, error: Exception) -> str:
        """エラーを分類"""
        error_str = str(error).lower()
        
        if 'timeout' in error_str or 'read timed out' in error_str:
            return 'timeout'
        elif 'rate limit' in error_str or '429' in error_str:
            return 'rate_limit'
        elif 'unauthorized' in error_str or '401' in error_str:
            return 'auth_error'
        elif 'forbidden' in error_str or '403' in error_str:
            return 'permission_error'
        elif 'not found' in error_str or '404' in error_str:
            return 'not_found'
        else:
            return 'unknown'
    
    def _calculate_wait_time(self, error_type: str, attempt: int) -> float:
        """エラータイプと試行回数に基づいて待機時間を計算"""
        base_wait = {
            'timeout': 5.0,
            'rate_limit': 10.0,
            'auth_error': 2.0,
            'permission_error': 1.0,
            'not_found': 1.0,
            'unknown': 3.0
        }
        
        wait_time = base_wait.get(error_type, 3.0)
        
        # 指数バックオフ
        wait_time *= (2 ** attempt)
        
        # タイムアウトが連続している場合は追加待機
        if error_type == 'timeout' and self.consecutive_timeouts > 0:
            wait_time += self.consecutive_timeouts * 5.0
        
        # 最大待機時間の制限
        return min(wait_time, 30.0)
    
    def get_current_playback_safe(self, spotify_client) -> Optional[Dict[str, Any]]:
        """安全な現在の再生状態取得"""
        return self.call_with_retry(
            lambda: spotify_client.current_playback(),
            "get_current_playback"
        )
    
    def start_playback_safe(self, spotify_client, uris: list) -> bool:
        """安全な再生開始"""
        result = self.call_with_retry(
            lambda: spotify_client.start_playback(uris=uris),
            "start_playback"
        )
        
        # デバッグログ: API経由での再生開始
        if result is not None and uris:
            try:
                import time
                time.sleep(0.3)  # 再生開始まで少し待つ
                current_playback = spotify_client.current_playback()
                if current_playback and current_playback.get('item'):
                    track_item = current_playback['item']
                    track_name = track_item.get('name', '不明')
                    artist_names = ", ".join([artist['name'] for artist in track_item.get('artists', [])]) if track_item.get('artists') else "不明"
                    print(f"[SPOTIFY_DEBUG] 🎵 API経由再生開始: 「{track_name}」 by {artist_names}")
            except Exception as e:
                print(f"[SPOTIFY_DEBUG] API経由再生デバッグログエラー: {e}")
        
        return result is not None
    
    def add_to_queue_safe(self, spotify_client, track_uri: str) -> bool:
        """安全なキュー追加"""
        result = self.call_with_retry(
            lambda: spotify_client.add_to_queue(track_uri),
            "add_to_queue"
        )
        return result is not None
    
    def get_health_status(self) -> Dict[str, Any]:
        """API接続の健全性状態を取得"""
        current_time = time.time()
        time_since_success = current_time - self.last_success_time
        
        return {
            'consecutive_timeouts': self.consecutive_timeouts,
            'time_since_last_success': time_since_success,
            'health_status': 'healthy' if time_since_success < 60 else 'degraded' if time_since_success < 300 else 'unhealthy'
        }


# グローバルインスタンス
_api_client = SpotifyAPIClient()


def get_api_client() -> SpotifyAPIClient:
    """SpotifyAPIClientのシングルトンインスタンスを取得"""
    return _api_client