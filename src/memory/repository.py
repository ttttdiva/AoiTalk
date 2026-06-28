"""
Repository layer for conversation memory data access
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple, Union
from sqlalchemy import select, delete, update, func, desc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db_session
from .models import ConversationSession, ConversationMessage, ConversationArchive, ConversationHistory
logger = logging.getLogger(__name__)


class ConversationRepository:
    """Repository for conversation data access"""
    
    def __init__(self, enable_search: bool = True):
        self.enable_search = enable_search
    
    async def create_session(self, user_id: str, character_name: str) -> ConversationSession:
        """Create a new conversation session
        
        Args:
            user_id: User identifier
            character_name: Character name
            
        Returns:
            ConversationSession: Created session
        """
        async with await get_db_session() as session:
            conv_session = ConversationSession(
                user_id=user_id,
                character_name=character_name
            )
            session.add(conv_session)
            await session.commit()
            await session.refresh(conv_session)
            return conv_session
    
    async def get_active_session(self, user_id: str, character_name: str) -> Optional[ConversationSession]:
        """Get active conversation session for user and character
        
        Args:
            user_id: User identifier
            character_name: Character name
            
        Returns:
            Optional[ConversationSession]: Active session if exists
        """
        async with await get_db_session() as session:
            stmt = select(ConversationSession).where(
                and_(
                    ConversationSession.user_id == user_id,
                    ConversationSession.character_name == character_name,
                    ConversationSession.is_active == True
                )
            ).order_by(desc(ConversationSession.last_activity))

            result = await session.execute(stmt)
            sessions = list(result.scalars().all())
            if not sessions:
                return None

            if len(sessions) > 1:
                extra_ids = [session_row.id for session_row in sessions[1:]]
                await session.execute(
                    update(ConversationSession).where(
                        ConversationSession.id.in_(extra_ids)
                    ).values(
                        is_active=False,
                        last_activity=datetime.utcnow()
                    )
                )
                await session.commit()
                logger.warning(
                    "Multiple active sessions found for user=%s character=%s; deactivated %d old sessions",
                    user_id,
                    character_name,
                    len(extra_ids)
                )

            return sessions[0]
    
    async def get_session_by_id(
        self,
        session_id: Union[str, uuid.UUID],
        with_messages: bool = False,
    ) -> Optional[ConversationSession]:
        """Get session by ID

        Args:
            session_id: Session identifier
            with_messages: Whether to eager load messages

        Returns:
            Optional[ConversationSession]: Session if exists
        """
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            stmt = select(ConversationSession).where(ConversationSession.id == session_id)
            if with_messages:
                stmt = stmt.options(selectinload(ConversationSession.messages))
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_session_title(
        self,
        session_id: Union[str, uuid.UUID],
        title: str,
        source: Optional[str] = None,
    ) -> bool:
        """Update the title for a conversation session."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            values = {"title": title, "last_activity": datetime.utcnow()}
            if source:
                result = await session.execute(
                    select(ConversationSession.context).where(
                        ConversationSession.id == session_id
                    )
                )
                current_context = result.scalar_one_or_none()
                context = (
                    dict(current_context) if isinstance(current_context, dict) else {}
                )
                context["title_generation"] = {"source": source}
                values["context"] = context

            stmt = (
                update(ConversationSession)
                .where(ConversationSession.id == session_id)
                .values(**values)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0
    
    async def update_session_activity(self, session_id: Union[str, uuid.UUID]):
        """Update session last activity timestamp
        
        Args:
            session_id: Session identifier
        """
        async with await get_db_session() as session:
            stmt = update(ConversationSession).where(
                ConversationSession.id == session_id
            ).values(last_activity=datetime.utcnow())
            
            await session.execute(stmt)
            await session.commit()
    
    async def deactivate_session(self, session_id: Union[str, uuid.UUID]):
        """Deactivate a session (mark as inactive)
        
        Args:
            session_id: Session identifier
        """
        # Convert string to UUID if needed
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)
            
        async with await get_db_session() as session:
            stmt = update(ConversationSession).where(
                ConversationSession.id == session_id
            ).values(is_active=False, last_activity=datetime.utcnow())
            
            await session.execute(stmt)
            await session.commit()
    
    async def _ensure_linear_parent_links(
        self, session: AsyncSession, session_id: Union[str, uuid.UUID]
    ) -> None:
        """Backfill parent links for old linear conversations.

        Older chat rows were stored as a flat list. Branching needs each
        message to point to the previous message in the active path.
        """
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
        )
        result = await session.execute(stmt)
        messages = list(result.scalars().all())
        previous: Optional[ConversationMessage] = None
        changed = False
        for message in messages:
            if message.is_active_branch is None:
                message.is_active_branch = True
                changed = True
            if previous and message.parent_message_id is None:
                message.parent_message_id = previous.id
                if message.branch_index is None:
                    message.branch_index = 0
                changed = True
            if message.is_active_branch:
                previous = message
        if changed:
            await session.flush()

    async def _deactivate_branch_from_message(
        self, session: AsyncSession, message_id: uuid.UUID
    ) -> None:
        await session.execute(
            update(ConversationMessage)
            .where(ConversationMessage.id == message_id)
            .values(is_active_branch=False)
        )
        child_rows = await session.execute(
            select(ConversationMessage).where(
                ConversationMessage.parent_message_id == message_id,
                ConversationMessage.is_active_branch == True,
            )
        )
        for child in child_rows.scalars().all():
            await self._deactivate_branch_from_message(session, child.id)

    async def _latest_active_message(
        self, session: AsyncSession, session_id: Union[str, uuid.UUID]
    ) -> Optional[ConversationMessage]:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.session_id == session_id,
                ConversationMessage.is_active_branch == True,
            )
            .order_by(desc(ConversationMessage.created_at), desc(ConversationMessage.id))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _count_siblings(
        self,
        session: AsyncSession,
        session_id: Union[str, uuid.UUID],
        parent_message_id: Optional[uuid.UUID],
    ) -> int:
        conditions = [ConversationMessage.session_id == session_id]
        if parent_message_id:
            conditions.append(ConversationMessage.parent_message_id == parent_message_id)
        else:
            conditions.append(ConversationMessage.parent_message_id.is_(None))
        result = await session.execute(select(func.count()).where(and_(*conditions)))
        return int(result.scalar() or 0)

    async def add_message(
        self,
        session_id: Union[str, uuid.UUID],
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        branch_from_message_id: Optional[str] = None,
        sender_type: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
    ) -> ConversationMessage:
        """Add message to conversation session
        
        Args:
            session_id: Session identifier
            role: Message role ('user' or 'assistant')
            content: Message content
            metadata: Optional metadata
            
        Returns:
            ConversationMessage: Created message
        """
        async with await get_db_session() as session:
            await self._ensure_linear_parent_links(session, session_id)

            parent_message_id: Optional[uuid.UUID] = None
            branch_index = 0
            if branch_from_message_id and role == "user":
                original_id = uuid.UUID(branch_from_message_id)
                original_result = await session.execute(
                    select(ConversationMessage).where(
                        ConversationMessage.id == original_id,
                        ConversationMessage.session_id == session_id,
                    )
                )
                original = original_result.scalar_one_or_none()
                if original is None:
                    raise ValueError(f"Message not found: {branch_from_message_id}")
                parent_message_id = original.parent_message_id
                await self._deactivate_branch_from_message(session, original.id)
                branch_index = await self._count_siblings(
                    session, session_id, parent_message_id
                )
            else:
                parent = await self._latest_active_message(session, session_id)
                parent_message_id = parent.id if parent else None
                branch_index = await self._count_siblings(
                    session, session_id, parent_message_id
                )

            message = ConversationMessage(
                session_id=session_id,
                role=role,
                content=content,
                # embedding removed - using Qdrant for vector search instead
                message_metadata=metadata or {},
                sender_type=sender_type,
                sender_id=sender_id,
                sender_display_name=sender_display_name,
                parent_message_id=parent_message_id,
                branch_index=branch_index,
                is_active_branch=True,
                token_count=len(content.split())  # Simple token estimation
            )
            
            session.add(message)
            
            # Update session message count and last activity
            await session.execute(
                update(ConversationSession).where(
                    ConversationSession.id == session_id
                ).values(
                    message_count=func.coalesce(
                        ConversationSession.message_count, 0
                    )
                    + 1,
                    last_activity=datetime.utcnow()
                )
            )
            
            await session.commit()
            await session.refresh(message)
            return message

    async def update_session_summary(
        self,
        session_id: Union[str, uuid.UUID],
        summary: str,
    ) -> bool:
        """Update the current summary stored on a conversation session."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_id)
            if not conversation:
                return False
            conversation.current_summary = summary
            conversation.last_activity = datetime.utcnow()
            await session.commit()
            return True
    
    async def get_session_messages(self, session_id: Union[str, uuid.UUID], limit: Optional[int] = None) -> List[ConversationMessage]:
        """Get messages for a session
        
        Args:
            session_id: Session identifier
            limit: Maximum number of messages to return
            
        Returns:
            List[ConversationMessage]: Session messages
        """
        async with await get_db_session() as session:
            stmt = select(ConversationMessage).where(
                ConversationMessage.session_id == session_id
            ).order_by(ConversationMessage.created_at)
            
            if limit:
                stmt = stmt.limit(limit)
            
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_active_branch_messages(self, session_id: Union[str, uuid.UUID]) -> List[ConversationMessage]:
        """Get messages on the currently active branch for a session."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            await self._ensure_linear_parent_links(session, session_id)
            stmt = (
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.is_active_branch == True,
                )
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_recent_messages(self, session_id: Union[str, uuid.UUID], count: int) -> List[ConversationMessage]:
        """Get recent messages from a session
        
        Args:
            session_id: Session identifier
            count: Number of recent messages to get
            
        Returns:
            List[ConversationMessage]: Recent messages
        """
        async with await get_db_session() as session:
            stmt = select(ConversationMessage).where(
                ConversationMessage.session_id == session_id
            ).order_by(desc(ConversationMessage.created_at)).limit(count)
            
            result = await session.execute(stmt)
            messages = result.scalars().all()
            return list(reversed(messages))  # Return in chronological order
    
    async def delete_old_messages(self, session_id: Union[str, uuid.UUID], keep_count: int) -> int:
        """Delete old messages from session, keeping the most recent ones
        
        Args:
            session_id: Session identifier
            keep_count: Number of recent messages to keep
            
        Returns:
            int: Number of messages deleted
        """
        async with await get_db_session() as session:
            # Get IDs of messages to keep
            keep_stmt = select(ConversationMessage.id).where(
                ConversationMessage.session_id == session_id
            ).order_by(desc(ConversationMessage.created_at)).limit(keep_count)
            
            keep_result = await session.execute(keep_stmt)
            keep_ids = [row[0] for row in keep_result.fetchall()]
            
            if not keep_ids:
                return 0

            await session.execute(
                update(ConversationMessage)
                .where(
                    and_(
                        ConversationMessage.session_id == session_id,
                        ConversationMessage.id.in_(keep_ids),
                        ConversationMessage.parent_message_id.is_not(None),
                        ~ConversationMessage.parent_message_id.in_(keep_ids),
                    )
                )
                .values(parent_message_id=None, branch_index=0)
            )
            
            # Delete messages not in keep list
            delete_stmt = delete(ConversationMessage).where(
                and_(
                    ConversationMessage.session_id == session_id,
                    ~ConversationMessage.id.in_(keep_ids)
                )
            )
            
            result = await session.execute(delete_stmt)
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == session_id)
                .values(message_count=len(keep_ids), last_activity=datetime.utcnow())
            )
            await session.commit()

            return result.rowcount
    
    async def create_archive(self, user_id: str, character_name: str, original_session_id: Union[str, uuid.UUID],
                           summary: str, message_count: int, start_time: datetime, 
                           end_time: datetime, metadata: Optional[Dict[str, Any]] = None) -> ConversationArchive:
        """Create conversation archive
        
        Args:
            user_id: User identifier
            character_name: Character name
            original_session_id: Original session ID
            summary: Conversation summary
            message_count: Number of messages summarized
            start_time: Start time of conversation
            end_time: End time of conversation
            metadata: Optional metadata
            
        Returns:
            ConversationArchive: Created archive
        """
        async with await get_db_session() as session:
            archive = ConversationArchive(
                user_id=user_id,
                character_name=character_name,
                original_session_id=str(original_session_id),
                summary=summary,
                message_count=message_count,
                start_time=start_time,
                end_time=end_time,
                message_metadata=metadata or {}
            )
            
            session.add(archive)
            await session.commit()
            await session.refresh(archive)
            return archive
    
    async def search_archives(self, user_id: str, character_name: str, 
                            query_embedding: List[float], similarity_threshold: float = 0.3,
                            limit: int = 5) -> List[Tuple[ConversationArchive, float]]:
        """Search conversation archives by semantic similarity
        
        Args:
            user_id: User identifier
            character_name: Character name
            query_embedding: Query embedding vector
            similarity_threshold: Minimum similarity score
            limit: Maximum results to return
            
        Returns:
            List[Tuple[ConversationArchive, float]]: Archives with similarity scores
        """
        return []
    
    async def add_to_history(self, user_id: str, session_id: str, character_name: str,
                           role: str, content: str, metadata: Optional[Dict[str, Any]] = None,
                           function_call_data: Optional[Dict[str, Any]] = None):
        """Add message to conversation history
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            character_name: Character name
            role: Message role
            content: Message content
            metadata: Optional metadata
            function_call_data: Optional function call data
        """
        async with await get_db_session() as session:
            history_entry = ConversationHistory(
                user_id=user_id,
                session_id=session_id,
                character_name=character_name,
                role=role,
                content=content,
                message_metadata=metadata or {},
                token_count=len(content.split()),  # Simple token estimation
                function_call_data=function_call_data
            )
            
            session.add(history_entry)
            await session.commit()
    
    async def cleanup_old_history(self, retention_days: int) -> int:
        """Clean up old conversation history
        
        Args:
            retention_days: Number of days to retain history
            
        Returns:
            int: Number of records deleted
        """
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        async with await get_db_session() as session:
            stmt = delete(ConversationHistory).where(
                ConversationHistory.created_at < cutoff_date
            )
            
            result = await session.execute(stmt)
            await session.commit()
            
            return result.rowcount
    
    async def get_message_by_id(self, message_id: str) -> Optional[ConversationMessage]:
        """Get message by ID
        
        Args:
            message_id: Message identifier
            
        Returns:
            ConversationMessage: Message object or None
        """
        async with await get_db_session() as session:
            stmt = select(ConversationMessage).where(ConversationMessage.id == message_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
    
    async def get_archive_by_id(self, archive_id: str) -> Optional[ConversationArchive]:
        """Get archive by ID
        
        Args:
            archive_id: Archive identifier
            
        Returns:
            ConversationArchive: Archive object or None
        """
        async with await get_db_session() as session:
            stmt = select(ConversationArchive).where(ConversationArchive.id == archive_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
