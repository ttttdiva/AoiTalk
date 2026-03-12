"""
Spotify Auto Queue Manager - シンプル版
曲終了時に自動的に新しい曲を追加
"""

import asyncio
import logging
import random
import time
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class AutoQueueManager:
    """シンプルな自動キュー管理"""
    
    def __init__(self):
        self.enabled = False
        self.search_query = ""
        self.search_type = "artist"  # "artist", "track", "genre"
        self.last_added_tracks: List[str] = []  # 重複防止用
        self.sync_failures: List[Dict[str, Any]] = []  # 同期失敗履歴
        self.user_id = "default"  # デフォルトユーザー
        self.character_name = "default"  # デフォルトキャラクター
        self.exclude_window_size = 20  # 除外ウィンドウサイズ（設定可能）
        self.exclude_hours = 2.0  # 除外時間窓（設定可能）
        
    def start_auto_queue(self, search_query: str, **kwargs):
        """自動キュー追加を開始"""
        self.enabled = True
        self.search_query = search_query
        # ユーザーとキャラクター情報を更新（提供された場合）
        if 'user_id' in kwargs:
            self.user_id = kwargs['user_id']
        if 'character_name' in kwargs:
            self.character_name = kwargs['character_name']
        if 'exclude_window_size' in kwargs:
            self.exclude_window_size = kwargs['exclude_window_size']
        if 'exclude_hours' in kwargs:
            self.exclude_hours = kwargs['exclude_hours']
        if 'search_type' in kwargs:
            self.search_type = kwargs['search_type']
        
        # 自動キュー開始時は履歴をクリア（新しいセッションとして扱う）
        self.last_added_tracks.clear()
        
        logger.info(f"Auto queue started for: {search_query}")
        # 監視システムが動作しているか確認
        from ...entertainment.spotify.monitoring import is_monitoring_active
        monitoring_status = is_monitoring_active()
        if not monitoring_status:
            logger.warning("監視システムが非アクティブです！自動追加が動作しません。")
        
        # 開始時に1曲追加
        initial_added = self.add_one_track()
        
        return {
            'status': 'started',
            'initial_track_added': initial_added
        }
    
    def stop_auto_queue(self):
        """自動キュー追加を停止"""
        self.enabled = False
        self.search_query = ""
        self.last_added_tracks.clear()
        
        logger.info("Auto queue stopped")
        return {'status': 'stopped'}
    
    def get_status(self):
        """現在の状態を取得"""
        return {
            'enabled': self.enabled,
            'config': {
                'search_query': self.search_query
            } if self.enabled else None
        }
    
    def is_enabled(self):
        """自動キューが有効かどうか"""
        return self.enabled
    
    def get_sync_failures(self) -> List[Dict[str, Any]]:
        """同期失敗履歴を取得"""
        return self.sync_failures.copy()
    
    def retry_failed_syncs(self) -> Dict[str, Any]:
        """失敗した同期を再試行"""
        if not self.sync_failures:
            return {'retried': 0, 'succeeded': 0, 'failed': 0}
        
        from ...entertainment.spotify.auth import get_spotify_manager
        manager = get_spotify_manager()
        if not manager:
            return {'error': 'Spotify manager not available'}
        
        user_spotify = manager._get_spotify_user_client()
        if not user_spotify:
            return {'error': 'Spotify user client not available'}
        
        retried = 0
        succeeded = 0
        still_failed = []
        
        for failure in self.sync_failures:
            try:
                # Spotifyキューに再追加を試行
                user_spotify.add_to_queue(failure['track_uri'])
                succeeded += 1
                logger.info(f"Sync retry succeeded: {failure['track_name']}")
            except Exception as e:
                # まだ失敗している場合は履歴を更新
                failure['retry_timestamp'] = time.time()
                failure['retry_error'] = str(e)
                still_failed.append(failure)
                logger.warning(f"Sync retry failed: {failure['track_name']} - {e}")
            
            retried += 1
        
        # 失敗履歴を更新（成功したものは削除）
        self.sync_failures = still_failed
        
        return {
            'retried': retried,
            'succeeded': succeeded, 
            'failed': len(still_failed)
        }
    
    def add_one_track(self) -> bool:
        """1曲だけキューに追加"""
        if not self.enabled or not self.search_query:
            logger.debug(f"Auto queue conditions not met: enabled={self.enabled}, search_query='{self.search_query}'")
            return False
            
        try:
            # Spotify APIで検索
            from ...entertainment.spotify.auth import get_spotify_manager
            manager = get_spotify_manager()
            if not manager:
                return False
                
            spotify = manager._get_spotify_client()
            if not spotify:
                return False
            
            # データベースから除外すべき曲を取得（非同期関数なので同期的に実行）
            excluded_tracks = self._get_excluded_tracks()
            logger.info(f"Excluding {len(excluded_tracks)} recently played tracks")
            
            # 検索タイプに応じて楽曲を取得
            if self.search_type == "genre":
                # ジャンルベース推薦を使用
                tracks = self._get_tracks_by_genre()
            else:
                # 従来のアーティスト/楽曲検索を使用
                tracks = self._get_tracks_by_search()
            
            if not tracks:
                logger.warning(f"No tracks found for: {self.search_query} (type: {self.search_type})")
                return False
            
            filtered_tracks = tracks
            
            # データベースとメモリ両方の履歴から重複を除外
            all_excluded = set(excluded_tracks) | set(self.last_added_tracks)
            new_tracks = [t for t in filtered_tracks if t['id'] not in all_excluded]
            
            if not new_tracks:
                # データベース履歴は保持したまま、メモリ履歴だけクリアして再試行
                logger.info("All tracks are in exclusion list, clearing memory cache and retrying")
                self.last_added_tracks.clear()
                new_tracks = [t for t in filtered_tracks if t['id'] not in excluded_tracks]
                
                if not new_tracks:
                    # それでも曲がない場合は、すべての曲から選択（最後の手段）
                    logger.warning("Still no tracks available, using all tracks as last resort")
                    new_tracks = filtered_tracks
            
            # ランダムに1曲選択
            track = random.choice(new_tracks)
            logger.info(f"Selected track: {track['name']} by {track['artists'][0]['name']}")
            
            # 自動キューは内部キューのみで制御（Spotifyキューとの競合を回避）
            user_spotify = manager._get_spotify_user_client()
            if user_spotify:
                # 内部キューにのみ追加
                from ...entertainment.spotify.queue_system import get_internal_queue
                internal_queue = get_internal_queue()
                track_info = {
                    'uri': track['uri'],
                    'name': track['name'],
                    'artist': ', '.join([artist['name'] for artist in track['artists']])
                }
                internal_queue.add(track_info)
                
                logger.info(f"Added to internal queue only: {track['name']} by {track['artists'][0]['name']}")
                queue_sync_success = True  # 内部キューのみなので常に成功
                
                self.last_added_tracks.append(track['id'])
                
                # 履歴は最新50曲まで保持
                if len(self.last_added_tracks) > 50:
                    self.last_added_tracks = self.last_added_tracks[-50:]
                
                # SpotifyLoggerに自動キューによる追加を記録（同期的に実行）
                self._log_auto_queue_activity_sync(track)
                
                # 内部キューのみなので同期失敗はなし
                logger.info(f"Auto-added to internal queue: {track['name']} by {track['artists'][0]['name']}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to add track: {e}")
            
        return False
    
    def _get_excluded_tracks(self) -> List[str]:
        """データベースから除外すべき曲のIDリストを取得（同期的に実行）"""
        try:
            from ...entertainment.spotify_logger import get_spotify_logger
            spotify_logger = get_spotify_logger()
            
            # 非同期関数を同期的に実行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                excluded_tracks = loop.run_until_complete(
                    spotify_logger.get_recent_tracks_for_exclusion(
                        user_id=self.user_id,
                        character_name=self.character_name,
                        window_size=self.exclude_window_size,
                        hours=self.exclude_hours
                    )
                )
                return excluded_tracks
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Failed to get excluded tracks from database: {e}")
            return []
    
    def _log_auto_queue_activity_sync(self, track: Dict[str, Any]):
        """自動キューによる追加をログに記録（同期版）"""
        try:
            from ...entertainment.spotify_logger import get_spotify_logger
            spotify_logger = get_spotify_logger()
            
            # 非同期関数を同期的に実行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    spotify_logger.log_activity(
                        user_id=self.user_id,
                        character_name=self.character_name,
                        action='add_to_queue',
                        track_info=track,
                        request_text=f"自動キュー: {self.search_query}",
                        success=True,
                        auto_queue=True,  # メタデータとして自動キューフラグを追加
                        search_query=self.search_query
                    )
                )
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Failed to log auto-queue activity: {e}")
    
    def _get_tracks_by_genre(self) -> List[Dict[str, Any]]:
        """ジャンルベースの楽曲取得"""
        try:
            from ...entertainment.spotify.recommendations import get_recommendations_engine
            engine = get_recommendations_engine()
            
            tracks = engine.get_tracks_by_genre(self.search_query, limit=50)
            logger.info(f"Retrieved {len(tracks)} tracks for genre: {self.search_query}")
            return tracks
            
        except Exception as e:
            logger.error(f"Failed to get tracks by genre: {e}")
            return []
    
    def _get_tracks_by_search(self) -> List[Dict[str, Any]]:
        """従来の検索ベース楽曲取得（アーティスト/楽曲）"""
        try:
            from ...entertainment.spotify.auth import get_spotify_manager
            manager = get_spotify_manager()
            spotify = manager._get_spotify_client()
            
            # 検索タイプに応じてクエリを構築
            if self.search_type == "artist":
                search_query = f"artist:{self.search_query}"
            else:
                # デフォルトはアーティスト検索（ジャンルの場合もここに来るが、実際はジャンル処理に行く）
                search_query = f"artist:{self.search_query}"
            
            logger.info(f"Auto-queue searching with: {search_query}")
            
            results = spotify.search(q=search_query, type='track', limit=50)
            if not results or 'tracks' not in results:
                logger.warning(f"No search results for: {search_query}")
                return []
                
            tracks = results['tracks']['items']
            if not tracks:
                logger.warning(f"No tracks found for: {search_query}")
                return []
            
            # アーティスト検索の場合は厳密フィルタリング（楽曲検索は削除済み）
            if self.search_type == "artist":
                query_lower = self.search_query.lower()
                filtered_tracks = []
                
                for track in tracks:
                    for artist in track['artists']:
                        if query_lower in artist['name'].lower():
                            filtered_tracks.append(track)
                            break
                
                if not filtered_tracks:
                    logger.warning(f"No tracks match artist filter: {self.search_query}")
                    return []
                
                return filtered_tracks
            
            return tracks
            
        except Exception as e:
            logger.error(f"Failed to get tracks by search: {e}")
            return []


# グローバルインスタンス
_auto_queue_manager = None


def get_auto_queue_manager() -> AutoQueueManager:
    """AutoQueueManagerのシングルトンインスタンスを取得"""
    global _auto_queue_manager
    if _auto_queue_manager is None:
        _auto_queue_manager = AutoQueueManager()
    return _auto_queue_manager