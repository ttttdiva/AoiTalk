"""
Discord session management
"""

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any

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
    assistant: Optional[Any] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    memory_prefilled: bool = False
    # ``id`` is an in-process runtime identity.  Persistent conversation rows
    # use a separate UUID managed by ConversationMemoryManager.
    conversation_id: Optional[str] = None
    runtime_id: Optional[str] = None
    # Incremented by /clear before rotating the durable row.  In-flight
    # resolver calls compare this token before publishing an awaited DB
    # result, preventing a pre-clear response from restoring the old ID.
    conversation_generation: int = 0
    # Set when /clear cannot create a replacement durable row.  While set,
    # resolver calls must not resurrect the previously-cleared active row.
    conversation_invalidated: bool = False

    def __post_init__(self) -> None:
        # Keep the historical ``id`` field for callers, while exposing an
        # explicit name for telemetry/turn correlation.
        self.runtime_id = self.runtime_id or self.id
    
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = datetime.now()


class SessionHandler:
    """Manage Discord user sessions"""

    def __init__(self, config=None):
        discord_config = config.get('discord', {}) if config else {}
        session_config = discord_config.get('session', {}) if isinstance(discord_config, dict) else {}
        self.sessions: Dict[str, DiscordSession] = {}  # session_key -> session
        self._lock = asyncio.Lock()
        self.default_mode = str(discord_config.get('default_mode', 'text')) if isinstance(discord_config, dict) else 'text'
        if self.default_mode not in {'text', 'voice'}:
            logger.warning("Invalid discord.default_mode '%s'; falling back to text", self.default_mode)
            self.default_mode = 'text'
        self.cleanup_interval = int(session_config.get('cleanup_interval', 300))
        self.inactive_timeout = int(session_config.get('inactive_timeout', 3600))

        # セッション自動クリーンアップタスク
        self._cleanup_task = None

    @staticmethod
    def memory_user_id(guild_id: Optional[int], user_id: Optional[int]) -> str:
        """Return the stable memory namespace used by Discord conversations."""
        parts = ["discord", str(guild_id) if guild_id is not None else "dm"]
        if user_id is not None:
            parts.append(str(user_id))
        return ":".join(parts)
        
    def _get_session_key(self, guild_id: int, user_id: int) -> str:
        """Get session key for guild/user combination"""
        return f"{guild_id}:{user_id}"
    
    async def get_or_create_session(
        self,
        guild_id: int,
        user_id: int,
        assistant: Any = None,
    ) -> DiscordSession:
        """Get existing session or create new one
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            
        Returns:
            DiscordSession instance
        """
        session_key = self._get_session_key(guild_id, user_id)
        session = None

        async with self._lock:
            if session_key in self.sessions:
                session = self.sessions[session_key]
                session.update_activity()
            else:
                # 新しいセッションを作成
                session = DiscordSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    mode=self.default_mode
                )
                self.sessions[session_key] = session

                logger.info(f"Created new session for user {user_id} in guild {guild_id}")

                # クリーンアップタスクを開始（まだ開始していない場合）
                if self._cleanup_task is None:
                    self._cleanup_task = asyncio.create_task(self._cleanup_inactive_sessions())

        if assistant is not None:
            await self.resolve_conversation_session(session, assistant)
        return session

    async def resolve_conversation_session(
        self,
        session: DiscordSession,
        assistant: Any,
    ) -> Optional[str]:
        """Resolve/create the durable ConversationSession for one Discord user.

        ``DiscordSession.id`` is intentionally never handed to the memory
        repository: it is generated at runtime and does not exist in the
        ``conversation_sessions`` table.  The memory manager owns lookup and
        creation, which also restores the active row after a bot restart.
        """
        if session is None or assistant is None:
            return getattr(session, "conversation_id", None)

        if getattr(session, "conversation_invalidated", False):
            # A prior /clear failed after invalidating the old DB identity.
            # Reusing get_or_create_session here could silently restore that
            # old row and violate the fail-safe boundary.
            return None

        memory_manager = getattr(getattr(assistant, "llm_client", None), "memory_manager", None)
        if memory_manager is None:
            return getattr(session, "conversation_id", None)

        worker_key = (session.guild_id, session.user_id)
        reset_events = getattr(assistant, "_session_reset_events", {})
        reset_event = reset_events.get(worker_key)
        if reset_event is not None and not reset_event.is_set():
            # A /clear barrier owns the session while it rotates the durable
            # row.  Resolve only after that operation publishes the new
            # identity, rather than racing its start_new_session call.
            await reset_event.wait()
        captured_generation = getattr(session, "conversation_generation", 0)
        captured_conversation_id = getattr(session, "conversation_id", None)

        character = (
            getattr(session, "character", None)
            or getattr(assistant, "character_name", None)
            or "Assistant"
        )
        memory_user_id = self.memory_user_id(session.guild_id, session.user_id)
        try:
            # ConversationMemoryManager.get_or_create_session initializes the
            # repository lazily and returns the latest active row.
            persistent = memory_manager.get_or_create_session(
                user_id=memory_user_id,
                character_name=character,
            )
            if inspect.isawaitable(persistent):
                persistent = await persistent
            if (
                getattr(session, "conversation_generation", 0) != captured_generation
                or getattr(session, "conversation_id", None) != captured_conversation_id
            ):
                logger.debug(
                    "Discarding stale Discord conversation resolution guild=%s user=%s",
                    session.guild_id,
                    session.user_id,
                )
                return getattr(session, "conversation_id", None)
            persistent_id = (
                getattr(persistent, "id", None)
                if persistent is not None and not isinstance(persistent, (str, bytes))
                else persistent
            )
            if persistent_id is not None:
                session.conversation_id = str(persistent_id)
                session.conversation_invalidated = False
                llm_client = getattr(assistant, "llm_client", None)
                if llm_client is not None and hasattr(llm_client, "current_session_id"):
                    llm_client.current_session_id = session.conversation_id
                set_identity = getattr(assistant, "set_session_identity", None)
                if callable(set_identity):
                    identity_kwargs = {
                        "user_id": session.user_id,
                        "guild_id": session.guild_id,
                        "session_id": session.conversation_id,
                        "runtime_session_id": (
                            getattr(session, "runtime_id", None)
                            or getattr(session, "id", None)
                        ),
                        "force": True,
                    }
                    try:
                        set_identity(**identity_kwargs)
                    except TypeError as exc:
                        if "force" not in str(exc):
                            raise
                        identity_kwargs.pop("force", None)
                        set_identity(**identity_kwargs)
                return session.conversation_id
        except Exception:
            # Discord must remain usable when optional persistence is offline;
            # generation itself will still proceed and the failure is visible.
            logger.warning(
                "Failed to resolve persistent Discord conversation session "
                "guild=%s user=%s",
                session.guild_id,
                session.user_id,
                exc_info=True,
            )
        return getattr(session, "conversation_id", None)

    async def ensure_conversation_session(
        self,
        session: DiscordSession,
        assistant: Any,
    ) -> Optional[str]:
        """Refresh the durable ID so ``/clear`` and restarts are respected."""
        return await self.resolve_conversation_session(session, assistant)
    
    async def get_session(self, guild_id: int, user_id: int) -> Optional[DiscordSession]:
        """Get existing session
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            
        Returns:
            DiscordSession instance or None
        """
        session_key = self._get_session_key(guild_id, user_id)
        session = None

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
        session = None

        async with self._lock:
            if session_key in self.sessions:
                session = self.sessions[session_key]
                
                del self.sessions[session_key]
                logger.info(f"Removed session for user {user_id} in guild {guild_id}")

        # Do not await arbitrary assistant cleanup while holding the session
        # registry lock; cleanup may call back into this handler.
        if session is not None and session.assistant:
            try:
                await session.assistant.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up assistant: {e}")
    
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
        # Atomically detach the snapshot before awaiting arbitrary assistant
        # cleanup.  Cleanup callbacks can create a replacement session for the
        # same guild/user; deleting by key after the await would otherwise
        # remove that new session as well.
        sessions_to_remove = []
        async with self._lock:
            for key, session in list(self.sessions.items()):
                if session.guild_id == guild_id:
                    sessions_to_remove.append((key, session))
                    self.sessions.pop(key, None)

        # ロックの外でクリーンアップ
        for _key, session in sessions_to_remove:
            if session.assistant:
                try:
                    await session.assistant.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up assistant: {e}")

        logger.info(f"Cleaned up {len(sessions_to_remove)} sessions for guild {guild_id}")
    
    async def _cleanup_inactive_sessions(self):
        """Periodically cleanup inactive sessions"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)

                inactive_threshold = self.inactive_timeout
                now = datetime.now()
                sessions_to_remove = []

                # Detach stale sessions atomically.  A new session may be
                # created while an old assistant's cleanup is awaiting I/O;
                # only the detached object should be cleaned up.
                async with self._lock:
                    for key, session in list(self.sessions.items()):
                        if (now - session.last_activity).total_seconds() > inactive_threshold:
                            sessions_to_remove.append((key, session))
                            self.sessions.pop(key, None)

                # ロックの外でクリーンアップ
                for _key, session in sessions_to_remove:
                    if session.assistant:
                        try:
                            await session.assistant.cleanup()
                        except Exception as e:
                            logger.error(f"Error cleaning up assistant: {e}")

                if sessions_to_remove:
                    logger.info(f"Cleaned up {len(sessions_to_remove)} inactive sessions")
                    
            except Exception as e:
                logger.error(f"Error in session cleanup task: {e}")
    
    async def shutdown(self):
        """Shutdown session handler and cleanup all sessions"""
        # クリーンアップタスクを停止
        cleanup_task = self._cleanup_task
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            finally:
                # Allow a handler instance to be reused after shutdown; a
                # cancelled task pointer must not suppress worker restart on
                # the next get_or_create_session call.
                if self._cleanup_task is cleanup_task:
                    self._cleanup_task = None
        
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
