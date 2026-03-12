from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime
import uuid


class BaseSession(ABC):
    """セッション基底クラス"""
    
    def __init__(self, 
                 session_id: Optional[str] = None,
                 mode: str = "unknown",
                 character: Optional[str] = None):
        """セッションを初期化
        
        Args:
            session_id: セッションID（指定しない場合は自動生成）
            mode: セッションモード（local, discord, web等）
            character: 使用するキャラクター名
        """
        self.session_id = session_id or str(uuid.uuid4())
        self.mode = mode
        self.character = character
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.context: Dict[str, Any] = {}
        self.is_active = True
        
        # セッション固有の設定
        self.settings: Dict[str, Any] = {
            'speech_rate': 1.0,
            'pitch': 0.0,
            'intonation': 1.0,
            'volume': 1.0,
            'auto_queue_enabled': False,
            'language': 'ja'
        }
        
    def update_activity(self):
        """最終アクティビティ時刻を更新"""
        self.last_activity = datetime.now()
        
    def get_setting(self, key: str, default: Any = None) -> Any:
        """設定値を取得
        
        Args:
            key: 設定キー
            default: デフォルト値
            
        Returns:
            設定値
        """
        return self.settings.get(key, default)
        
    def set_setting(self, key: str, value: Any):
        """設定値を更新
        
        Args:
            key: 設定キー
            value: 設定値
        """
        self.settings[key] = value
        self.update_activity()
        
    def update_context(self, context: Dict[str, Any]):
        """コンテキストを更新
        
        Args:
            context: 更新するコンテキスト辞書
        """
        self.context.update(context)
        self.update_activity()
        
    def clear_context(self):
        """コンテキストをクリア"""
        self.context.clear()
        self.update_activity()
        
    @abstractmethod
    async def initialize(self) -> bool:
        """セッションを初期化（非同期）
        
        Returns:
            初期化が成功したかどうか
        """
        pass
        
    @abstractmethod
    async def cleanup(self):
        """セッションをクリーンアップ（非同期）"""
        pass
        
    @abstractmethod
    def get_user_info(self) -> Dict[str, Any]:
        """ユーザー情報を取得
        
        Returns:
            ユーザー情報の辞書
        """
        pass
        
    def to_dict(self) -> Dict[str, Any]:
        """セッション情報を辞書形式で取得
        
        Returns:
            セッション情報
        """
        return {
            'session_id': self.session_id,
            'mode': self.mode,
            'character': self.character,
            'created_at': self.created_at.isoformat(),
            'last_activity': self.last_activity.isoformat(),
            'is_active': self.is_active,
            'settings': self.settings.copy(),
            'user_info': self.get_user_info()
        }