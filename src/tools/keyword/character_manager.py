"""
キャラクター切り替え管理システム
各コンポーネント間でキャラクター切り替えを同期するための中央管理システム
"""

from typing import Optional, Callable, List, Dict, Any
import logging
import threading

from .selection_mode_state import get_selection_mode_state

logger = logging.getLogger(__name__)


class CharacterSwitchManager:
    """キャラクター切り替えの中央管理クラス"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """シングルトンパターンの実装"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初期化"""
        if self._initialized:
            return
        
        self._initialized = True
        self._callbacks: List[Callable[[str, str], None]] = []
        self._current_character = "ずんだもん"
        self._current_yaml = "zundamon"
        
    def register_callback(self, callback: Callable[[str, str], None]) -> None:
        """キャラクター切り替え時のコールバックを登録
        
        Args:
            callback: キャラクター名とYAMLファイル名を受け取るコールバック関数
        """
        if callback not in self._callbacks:
            self._callbacks.append(callback)
            logger.debug(f"コールバック登録: {callback.__name__ if hasattr(callback, '__name__') else callback}")
    
    def unregister_callback(self, callback: Callable[[str, str], None]) -> None:
        """コールバックの登録を解除
        
        Args:
            callback: 登録解除するコールバック関数
        """
        if callback in self._callbacks:
            self._callbacks.remove(callback)
            logger.debug(f"コールバック解除: {callback.__name__ if hasattr(callback, '__name__') else callback}")
    
    def switch_character(self, character_name: str, yaml_filename: str) -> bool:
        """キャラクターを切り替え、全コンポーネントに通知
        
        Args:
            character_name: 新しいキャラクター名
            yaml_filename: YAMLファイル名（拡張子なし）
            
        Returns:
            切り替えが成功したかどうか
        """
        try:
            logger.info(f"キャラクター切り替え開始: {self._current_character} -> {character_name}")

            # If selection mode (voice-driven switching) is active, forcefully exit
            selection_state = get_selection_mode_state()
            if selection_state.active:
                logger.info("キャラクター選択モードを解除します（外部操作による切り替え）")
                selection_state.deactivate()
            
            # 現在のキャラクターを更新
            old_character = self._current_character
            old_yaml = self._current_yaml
            
            self._current_character = character_name
            self._current_yaml = yaml_filename
            
            # 全てのコールバックを実行
            success_count = 0
            for callback in self._callbacks:
                try:
                    callback(character_name, yaml_filename)
                    success_count += 1
                except Exception as e:
                    logger.error(f"コールバック実行エラー ({callback}): {e}")
                    # エラーがあっても他のコールバックは実行する
            
            logger.info(f"キャラクター切り替え完了: {success_count}/{len(self._callbacks)} コンポーネントが更新されました")
            
            return success_count > 0
            
        except Exception as e:
            logger.error(f"キャラクター切り替えエラー: {e}")
            return False
    
    def get_current_character(self) -> str:
        """現在のキャラクター名を取得
        
        Returns:
            現在のキャラクター名
        """
        return self._current_character
    
    def get_current_yaml(self) -> str:
        """現在のYAMLファイル名を取得
        
        Returns:
            現在のYAMLファイル名（拡張子なし）
        """
        return self._current_yaml


# グローバルインスタンスを作成
_character_manager = CharacterSwitchManager()


def get_character_manager() -> CharacterSwitchManager:
    """キャラクター切り替えマネージャーのインスタンスを取得
    
    Returns:
        CharacterSwitchManagerのシングルトンインスタンス
    """
    return _character_manager
