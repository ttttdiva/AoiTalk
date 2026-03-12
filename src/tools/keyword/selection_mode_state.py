"""
選択モードのグローバル状態管理
"""

import threading
from typing import Optional, Callable, List
import logging

logger = logging.getLogger(__name__)


class SelectionModeState:
    """選択モードのグローバル状態管理クラス"""
    
    def __init__(self):
        """初期化"""
        self._active = False
        self._lock = threading.Lock()
        self._observers: List[Callable[[bool], None]] = []
        logger.info(f"[SelectionModeState] インスタンスを作成しました (ID: {id(self)})")
    
    @property
    def active(self) -> bool:
        """選択モードがアクティブかどうか"""
        with self._lock:
            return self._active
    
    def activate(self) -> None:
        """選択モードをアクティブにする"""
        with self._lock:
            previous = self._active
            self._active = True
            logger.info(f"[SelectionModeState] 選択モードをアクティブにしました: {previous} -> {self._active} (ID: {id(self)})")
            
        # オブザーバーに通知
        self._notify_observers(True)
    
    def deactivate(self) -> None:
        """選択モードを非アクティブにする"""
        with self._lock:
            previous = self._active
            self._active = False
            logger.info(f"[SelectionModeState] 選択モードを非アクティブにしました: {previous} -> {self._active} (ID: {id(self)})")
            
        # オブザーバーに通知
        self._notify_observers(False)
    
    def register_observer(self, callback: Callable[[bool], None]) -> None:
        """状態変化を監視するコールバックを登録
        
        Args:
            callback: 状態変化時に呼ばれる関数 (引数: 新しい状態)
        """
        with self._lock:
            if callback not in self._observers:
                self._observers.append(callback)
                logger.info(f"[SelectionModeState] オブザーバーを登録しました")
    
    def unregister_observer(self, callback: Callable[[bool], None]) -> None:
        """オブザーバーを登録解除
        
        Args:
            callback: 登録解除する関数
        """
        with self._lock:
            if callback in self._observers:
                self._observers.remove(callback)
                logger.info(f"[SelectionModeState] オブザーバーを登録解除しました")
    
    def _notify_observers(self, new_state: bool) -> None:
        """全てのオブザーバーに状態変化を通知
        
        Args:
            new_state: 新しい状態
        """
        observers = []
        with self._lock:
            observers = self._observers.copy()
        
        for observer in observers:
            try:
                observer(new_state)
            except Exception as e:
                logger.error(f"[SelectionModeState] オブザーバー通知エラー: {e}")


# グローバルインスタンス
_selection_mode_state: Optional[SelectionModeState] = None


def get_selection_mode_state() -> SelectionModeState:
    """選択モード状態のシングルトンインスタンスを取得"""
    global _selection_mode_state
    if _selection_mode_state is None:
        _selection_mode_state = SelectionModeState()
        logger.info(f"[SelectionModeState] グローバルインスタンスを作成しました")
    return _selection_mode_state


def reset_selection_mode_state() -> None:
    """選択モード状態をリセット（主にテスト用）"""
    global _selection_mode_state
    if _selection_mode_state is not None:
        _selection_mode_state.deactivate()
        logger.info(f"[SelectionModeState] グローバル状態をリセットしました")