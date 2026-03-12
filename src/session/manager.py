import asyncio
from typing import Dict, Optional, List, Union
from datetime import datetime, timedelta
import threading
from .base import BaseSession
from .local_session import LocalSession
from .discord_session import DiscordSession


class SessionManager:
    """統一セッション管理システム"""
    
    def __init__(self, max_sessions: int = 100, 
                 session_timeout: int = 3600):
        """セッションマネージャーを初期化
        
        Args:
            max_sessions: 最大セッション数
            session_timeout: セッションタイムアウト（秒）
        """
        self.sessions: Dict[str, BaseSession] = {}
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self._lock = threading.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        
    async def create_local_session(self, 
                                 character: Optional[str] = None) -> LocalSession:
        """ローカルセッションを作成
        
        Args:
            character: 使用するキャラクター名
            
        Returns:
            作成されたLocalSession
            
        Raises:
            RuntimeError: セッション数が上限に達している場合
        """
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                # 古いセッションをクリーンアップ
                await self._cleanup_expired_sessions()
                
                # それでも上限に達している場合はエラー
                if len(self.sessions) >= self.max_sessions:
                    raise RuntimeError(f"Maximum number of sessions ({self.max_sessions}) reached")
                    
        # ローカルセッションを作成
        session = LocalSession(character=character)
        
        # 初期化
        if await session.initialize():
            with self._lock:
                self.sessions[session.session_id] = session
                
            # クリーンアップタスクを開始（まだ実行されていない場合）
            if not self._cleanup_task or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
                
            return session
        else:
            raise RuntimeError("Failed to initialize local session")
            
    async def create_discord_session(self,
                                   user,
                                   guild=None,
                                   channel=None,
                                   character: Optional[str] = None) -> DiscordSession:
        """Discordセッションを作成
        
        Args:
            user: Discordユーザー
            guild: Discordギルド
            channel: Discord音声チャンネル
            character: 使用するキャラクター名
            
        Returns:
            作成されたDiscordSession
            
        Raises:
            RuntimeError: セッション数が上限に達している場合
        """
        # 既存のセッションを確認
        session_id = f"discord_{user.id}_{guild.id if guild else 'dm'}"
        existing = self.get_session(session_id)
        if existing and isinstance(existing, DiscordSession):
            # 既存セッションを更新
            existing.update_activity()
            if channel:
                await existing.update_voice_channel(channel)
            return existing
            
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                # 古いセッションをクリーンアップ
                await self._cleanup_expired_sessions()
                
                if len(self.sessions) >= self.max_sessions:
                    raise RuntimeError(f"Maximum number of sessions ({self.max_sessions}) reached")
                    
        # Discordセッションを作成
        session = DiscordSession(
            user=user,
            guild=guild,
            channel=channel,
            character=character
        )
        
        # 初期化
        if await session.initialize():
            with self._lock:
                self.sessions[session.session_id] = session
                
            # クリーンアップタスクを開始
            if not self._cleanup_task or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
                
            return session
        else:
            raise RuntimeError("Failed to initialize Discord session")
            
    def get_session(self, session_id: str) -> Optional[BaseSession]:
        """セッションIDでセッションを取得
        
        Args:
            session_id: セッションID
            
        Returns:
            セッションまたはNone
        """
        with self._lock:
            session = self.sessions.get(session_id)
            if session and session.is_active:
                return session
            return None
            
    def get_active_sessions(self, mode: Optional[str] = None) -> List[BaseSession]:
        """アクティブなセッションのリストを取得
        
        Args:
            mode: フィルタリングするモード（local, discord等）
            
        Returns:
            アクティブなセッションのリスト
        """
        with self._lock:
            sessions = [s for s in self.sessions.values() if s.is_active]
            if mode:
                sessions = [s for s in sessions if s.mode == mode]
            return sessions
            
    async def close_session(self, session_id: str):
        """セッションを閉じる
        
        Args:
            session_id: セッションID
        """
        with self._lock:
            session = self.sessions.get(session_id)
            
        if session:
            await session.cleanup()
            with self._lock:
                del self.sessions[session_id]
                
    async def close_all_sessions(self):
        """すべてのセッションを閉じる"""
        # クリーンアップタスクを停止
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            
        # すべてのセッションをクリーンアップ
        sessions_to_close = []
        with self._lock:
            sessions_to_close = list(self.sessions.values())
            
        for session in sessions_to_close:
            await session.cleanup()
            
        with self._lock:
            self.sessions.clear()
            
    async def _cleanup_expired_sessions(self):
        """期限切れセッションをクリーンアップ"""
        current_time = datetime.now()
        timeout_delta = timedelta(seconds=self.session_timeout)
        
        expired_sessions = []
        with self._lock:
            for session_id, session in self.sessions.items():
                if current_time - session.last_activity > timeout_delta:
                    expired_sessions.append(session_id)
                    
        # 期限切れセッションを削除
        for session_id in expired_sessions:
            await self.close_session(session_id)
            print(f"[SessionManager] Cleaned up expired session: {session_id}")
            
    async def _periodic_cleanup(self):
        """定期的なクリーンアップタスク"""
        while True:
            try:
                # 30分ごとにクリーンアップ
                await asyncio.sleep(1800)
                await self._cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[SessionManager] Error during cleanup: {e}")
                
    def get_statistics(self) -> Dict[str, any]:
        """セッション統計を取得
        
        Returns:
            統計情報の辞書
        """
        with self._lock:
            total_sessions = len(self.sessions)
            active_sessions = len([s for s in self.sessions.values() if s.is_active])
            
            mode_counts = {}
            for session in self.sessions.values():
                mode_counts[session.mode] = mode_counts.get(session.mode, 0) + 1
                
        return {
            'total_sessions': total_sessions,
            'active_sessions': active_sessions,
            'max_sessions': self.max_sessions,
            'session_timeout': self.session_timeout,
            'mode_counts': mode_counts
        }