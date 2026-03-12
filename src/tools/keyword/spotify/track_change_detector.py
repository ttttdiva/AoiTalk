"""
Spotify Track Change Detection Module
楽曲変更検知の専用モジュール
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class TrackChangeDetector:
    """楽曲変更検知専用クラス"""
    
    def __init__(self):
        self.last_track_id: Optional[str] = None
        self.near_completion_detected: bool = False
        self.last_progress_check: int = 0
    
    def reset_tracking(self, current_track_id: str, progress_ms: int = 0):
        """追跡状態をリセット"""
        self.last_track_id = current_track_id
        self.near_completion_detected = False
        self.last_progress_check = progress_ms
        logger.info(f"[TrackDetector] Tracking reset: track_id={current_track_id}")
    
    def check_track_change(self, current_track_id: str, progress_ms: int, duration_ms: int) -> Dict[str, Any]:
        """
        楽曲変更をチェックし、変更タイプを判定
        
        Returns:
            {
                'changed': bool,
                'change_type': 'natural_completion' | 'manual_skip' | 'forced_change',
                'near_completion': bool,
                'completion_ratio': float
            }
        """
        result = {
            'changed': False,
            'change_type': None,
            'near_completion': False,
            'completion_ratio': 0.0
        }
        
        # 完了率計算
        if duration_ms > 0:
            completion_ratio = progress_ms / duration_ms
            result['completion_ratio'] = completion_ratio
        else:
            completion_ratio = 0.0
        
        # 楽曲完了接近の検知
        if completion_ratio >= 0.9 and not self.near_completion_detected:
            self.near_completion_detected = True
            result['near_completion'] = True
            logger.info(f"[TrackDetector] Near completion detected: {completion_ratio:.1%}")
        
        # 楽曲変更の検知
        if self.last_track_id != current_track_id:
            result['changed'] = True
            
            # 変更タイプの判定
            if self.near_completion_detected:
                result['change_type'] = 'natural_completion'
                logger.info(f"[TrackDetector] Natural completion: {self.last_track_id} → {current_track_id}")
            else:
                result['change_type'] = 'manual_skip'
                logger.info(f"[TrackDetector] Manual skip: {self.last_track_id} → {current_track_id}")
            
            # デバッグログ: 楽曲変更時の現在再生中楽曲情報を出力
            try:
                # SpotifyマネージャーからAPIクライアントを取得して現在の再生情報を取得
                from ...entertainment.spotify.auth import get_spotify_manager
                manager = get_spotify_manager()
                if manager:
                    spotify = manager._get_spotify_user_client()
                    if spotify:
                        current_playback = spotify.current_playback()
                        if current_playback and current_playback.get('item'):
                            track_item = current_playback['item']
                            track_name = track_item.get('name', '不明')
                            artist_names = ", ".join([artist['name'] for artist in track_item.get('artists', [])]) if track_item.get('artists') else "不明"
                            change_type_jp = "自然終了" if result['change_type'] == 'natural_completion' else "手動スキップ"
                            print(f"[SPOTIFY_DEBUG] 🔄 楽曲変更({change_type_jp}): 「{track_name}」 by {artist_names}")
                        else:
                            print(f"[SPOTIFY_DEBUG] 🔄 楽曲変更: 再生情報取得失敗")
            except Exception as e:
                print(f"[SPOTIFY_DEBUG] 楽曲変更デバッグログエラー: {e}")
            
            # 新しい楽曲の追跡開始
            self.reset_tracking(current_track_id, progress_ms)
        
        # 進行状況の更新（デバッグ用）
        if abs(progress_ms - self.last_progress_check) > 10000:  # 10秒毎
            self.last_progress_check = progress_ms
            logger.debug(f"[TrackDetector] Progress: {completion_ratio:.1%}")
        
        return result


# グローバルインスタンス
_track_detector = TrackChangeDetector()


def get_track_detector() -> TrackChangeDetector:
    """TrackChangeDetectorのシングルトンインスタンスを取得"""
    return _track_detector


def reset_track_detection(track_id: str):
    """追跡状態をリセット（外部インターフェース）"""
    _track_detector.reset_tracking(track_id)