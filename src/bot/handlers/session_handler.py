"""
Discord session management
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any

from ..modes.discord_mode import DiscordMode

logger = logging.getLogger(__name__)


@dataclass
class DiscordSession:
    """Discord user session"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    guild_id: int = None
    user_id: int = None
    voice_channel_id: Optional[int] = None
    mode: str = 'text'  # 'text' or 'voice'
    character: Optional[str] = None
    assistant: Optional[DiscordMode] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    memory_prefilled: bool = False
    
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = datetime.now()


class SessionHandler:
    """Manage Discord user sessions"""
    
    def __init__(self):
        self.sessions: Dict[str, DiscordSession] = {}  # session_key -> session
        self._lock = asyncio.Lock()
        
        # セッション自動クリーンアップタスク
        self._cleanup_task = None
        
    def _get_session_key(self, guild_id: int, user_id: int) -> str:
        """Get session key for guild/user combination"""
        return f"{guild_id}:{user_id}"
    
    async def get_or_create_session(self, guild_id: int, user_id: int) -> DiscordSession:
        """Get existing session or create new one
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            
        Returns:
            DiscordSession instance
        """
        session_key = self._get_session_key(guild_id, user_id)
        
        async with self._lock:
            if session_key in self.sessions:
                session = self.sessions[session_key]
                session.update_activity()
                return session

            # 新しいセッションを作成
            session = DiscordSession(
                guild_id=guild_id,
                user_id=user_id
            )
            self.sessions[session_key] = session
            
            logger.info(f"Created new session for user {user_id} in guild {guild_id}")
            
            # クリーンアップタスクを開始（まだ開始していない場合）
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._cleanup_inactive_sessions())
            
            return session
    
    async def get_session(self, guild_id: int, user_id: int) -> Optional[DiscordSession]:
        """Get existing session
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            
        Returns:
            DiscordSession instance or None
        """
        session_key = self._get_session_key(guild_id, user_id)
        
        async with self._lock:
            session = self.sessions.get(session_key)
            if session:
                session.update_activity()
            return session
    
    async def remove_session(self, guild_id: int, user_id: int):
        """Remove session
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
        """
        session_key = self._get_session_key(guild_id, user_id)
        
        async with self._lock:
            if session_key in self.sessions:
                session = self.sessions[session_key]
                
                # アシスタントのクリーンアップ
                if session.assistant:
                    try:
                        await session.assistant.cleanup()
                    except Exception as e:
                        logger.error(f"Error cleaning up assistant: {e}")
                
                del self.sessions[session_key]
                logger.info(f"Removed session for user {user_id} in guild {guild_id}")
    
    async def get_guild_sessions(self, guild_id: int) -> Dict[int, DiscordSession]:
        """Get all sessions for a guild
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Dictionary of user_id -> session
        """
        guild_sessions = {}
        
        async with self._lock:
            for key, session in self.sessions.items():
                if session.guild_id == guild_id:
                    guild_sessions[session.user_id] = session
        
        return guild_sessions
    
    async def cleanup_guild_sessions(self, guild_id: int):
        """Remove all sessions for a guild
        
        Args:
            guild_id: Discord guild ID
        """
        sessions_to_remove = []
        
        async with self._lock:
            for key, session in self.sessions.items():
                if session.guild_id == guild_id:
                    sessions_to_remove.append((key, session))
        
        # ロックの外でクリーンアップ
        for key, session in sessions_to_remove:
            if session.assistant:
                try:
                    await session.assistant.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up assistant: {e}")
        
        # セッションを削除
        async with self._lock:
            for key, _ in sessions_to_remove:
                if key in self.sessions:
                    del self.sessions[key]
        
        logger.info(f"Cleaned up {len(sessions_to_remove)} sessions for guild {guild_id}")
    
    async def _cleanup_inactive_sessions(self):
        """Periodically cleanup inactive sessions"""
        while True:
            try:
                await asyncio.sleep(300)  # 5分ごとにチェック
                
                inactive_threshold = 3600  # 1時間
                now = datetime.now()
                sessions_to_remove = []
                
                async with self._lock:
                    for key, session in self.sessions.items():
                        if (now - session.last_activity).total_seconds() > inactive_threshold:
                            sessions_to_remove.append((key, session))
                
                # ロックの外でクリーンアップ
                for key, session in sessions_to_remove:
                    if session.assistant:
                        try:
                            await session.assistant.cleanup()
                        except Exception as e:
                            logger.error(f"Error cleaning up assistant: {e}")
                
                # セッションを削除
                async with self._lock:
                    for key, _ in sessions_to_remove:
                        if key in self.sessions:
                            del self.sessions[key]
                
                if sessions_to_remove:
                    logger.info(f"Cleaned up {len(sessions_to_remove)} inactive sessions")
                    
            except Exception as e:
                logger.error(f"Error in session cleanup task: {e}")
    
    async def shutdown(self):
        """Shutdown session handler and cleanup all sessions"""
        # クリーンアップタスクを停止
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # すべてのセッションをクリーンアップ
        sessions_to_cleanup = []
        async with self._lock:
            sessions_to_cleanup = list(self.sessions.values())
            self.sessions.clear()
        
        # アシスタントのクリーンアップ
        for session in sessions_to_cleanup:
            if session.assistant:
                try:
                    await session.assistant.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up assistant: {e}")
        
        logger.info("Session handler shutdown complete")
