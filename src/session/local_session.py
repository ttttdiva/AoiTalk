from typing import Dict, Any, Optional
import os
import getpass
from .base import BaseSession


class LocalSession(BaseSession):
    """ローカルモード用セッション"""
    
    def __init__(self, 
                 character: Optional[str] = None,
                 session_id: Optional[str] = None):
        """ローカルセッションを初期化
        
        Args:
            character: 使用するキャラクター名
            session_id: セッションID（オプション）
        """
        super().__init__(
            session_id=session_id,
            mode="local",
            character=character
        )
        
        # ローカル固有の情報
        self.username = getpass.getuser()
        self.hostname = os.uname().nodename if hasattr(os, 'uname') else 'localhost'
        
    async def initialize(self) -> bool:
        """セッションを初期化
        
        Returns:
            初期化が成功したかどうか
        """
        # ローカルセッションの初期化処理
        # 必要に応じて設定ファイルの読み込みなどを行う
        try:
            # ユーザー固有の設定を読み込む（将来的な拡張用）
            # self._load_user_preferences()
            
            return True
        except Exception as e:
            print(f"[LocalSession] Failed to initialize: {e}")
            return False
            
    async def cleanup(self):
        """セッションをクリーンアップ"""
        # ローカルセッションのクリーンアップ処理
        # 必要に応じて一時ファイルの削除などを行う
        self.is_active = False
        
    def get_user_info(self) -> Dict[str, Any]:
        """ユーザー情報を取得
        
        Returns:
            ユーザー情報の辞書
        """
        return {
            'username': self.username,
            'hostname': self.hostname,
            'type': 'local'
        }
        
    def _load_user_preferences(self):
        """ユーザー設定を読み込む（将来的な実装用）"""
        # ホームディレクトリから設定ファイルを読み込む
        # 例: ~/.aoitalk/preferences.json
        pass