"""
Auto Queue Trigger Logic Module
自動キュー追加判定の専用モジュール
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class AutoQueueTrigger:
    """自動キュー追加判定専用クラス"""
    
    def __init__(self):
        self.last_added_track_id = None  # 最後に追加した楽曲のID（重複防止）
        self.preload_completed = False   # 事前読み込み完了フラグ
    
    def should_add_track(self, change_type: str, queue_size: int, auto_queue_enabled: bool) -> Dict[str, Any]:
        """
        自動キューに楽曲を追加するべきかを判定
        
        Args:
            change_type: 楽曲変更タイプ ('natural_completion' | 'manual_skip' | 'forced_change')
            queue_size: 現在の内部キューサイズ
            auto_queue_enabled: 自動キュー機能が有効かどうか
            
        Returns:
            {
                'should_add': bool,
                'reason': str,
                'trigger_type': str
            }
        """
        result = {
            'should_add': False,
            'reason': '',
            'trigger_type': ''
        }
        
        # 基本条件チェック
        if not auto_queue_enabled:
            result['reason'] = 'Auto queue disabled'
            return result
        
        if queue_size > 1:
            result['reason'] = f'Queue has enough tracks ({queue_size} > 1)'
            return result
        
        # 変更タイプ別判定
        if change_type == 'natural_completion':
            # 事前読み込みが完了している場合は、completion時の追加をスキップ
            if self.preload_completed:
                result['should_add'] = False
                result['reason'] = 'Already preloaded for this track'
            else:
                result['should_add'] = True
                result['reason'] = 'Natural track completion (no preload)'
            result['trigger_type'] = 'completion'
            
        elif change_type == 'manual_skip':
            result['should_add'] = True
            result['reason'] = 'Manual skip detected'
            result['trigger_type'] = 'skip'
            
        else:
            result['reason'] = f'Unknown change type: {change_type}'
        
        return result
    
    def on_track_change(self, new_track_id: str):
        """楽曲変更時の状態リセット"""
        self.preload_completed = False
        logger.debug(f"Track change detected, reset preload flag for: {new_track_id}")
    
    def should_preload_track(self, completion_ratio: float, queue_size: int, auto_queue_enabled: bool) -> Dict[str, Any]:
        """
        楽曲完了前の事前追加が必要かを判定
        
        Args:
            completion_ratio: 完了率 (0.0-1.0)
            queue_size: 現在の内部キューサイズ
            auto_queue_enabled: 自動キュー機能が有効かどうか
            
        Returns:
            {
                'should_preload': bool,
                'reason': str
            }
        """
        result = {
            'should_preload': False,
            'reason': ''
        }
        
        # 基本条件チェック
        if not auto_queue_enabled:
            result['reason'] = 'Auto queue disabled'
            return result
        
        if queue_size > 1:
            result['reason'] = f'Queue has enough tracks ({queue_size} > 1)'
            return result
        
        if completion_ratio >= 0.9 and not self.preload_completed:
            result['should_preload'] = True
            result['reason'] = f'Near completion ({completion_ratio:.1%})'
            self.preload_completed = True  # 事前読み込み完了をマーク
        elif self.preload_completed:
            result['reason'] = f'Preload already completed ({completion_ratio:.1%})'
        else:
            result['reason'] = f'Not near completion ({completion_ratio:.1%})'
        
        return result
    
    def add_track_and_log(self, trigger_type: str) -> bool:
        """
        実際に楽曲追加を実行しログを記録
        
        Args:
            trigger_type: トリガータイプ ('completion' | 'skip' | 'preload')
            
        Returns:
            追加が成功したかどうか
        """
        try:
            from .auto_queue_manager import get_auto_queue_manager
            auto_queue = get_auto_queue_manager()
            
            added = auto_queue.add_one_track()
            
            if added:
                logger.info(f"[AutoQueueTrigger] Track added successfully: trigger={trigger_type}")
                print(f"[DEBUG] ✅ 自動キューから1曲追加成功: {trigger_type}")
            else:
                logger.warning(f"[AutoQueueTrigger] Track addition failed: trigger={trigger_type}")
                print(f"[DEBUG] ❌ 自動キューから1曲追加失敗: {trigger_type}")
            
            return added
            
        except Exception as e:
            logger.error(f"[AutoQueueTrigger] Exception during track addition: {e}")
            print(f"[DEBUG] 自動キュー追加例外: {e}")
            return False


# グローバルインスタンス
_auto_queue_trigger = AutoQueueTrigger()


def get_auto_queue_trigger() -> AutoQueueTrigger:
    """AutoQueueTriggerのシングルトンインスタンスを取得"""
    return _auto_queue_trigger