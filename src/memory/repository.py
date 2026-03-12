"""
Repository layer for conversation memory data access
"""

import asyncio
import uuid
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple, Union
from sqlalchemy import select, delete, update, func, desc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db_session
from .models import ConversationSession, ConversationMessage, ConversationArchive, ConversationHistory
from .embedding import get_embedding_manager

logger = logging.getLogger(__name__)


class ConversationRepository:
    """Repository for conversation data access"""
    
    def __init__(self, enable_search: bool = True):
        self.enable_search = enable_search
        self._embedding_manager = None
    
    @property
    def embedding_manager(self):
        """Lazy initialization of embedding manager"""
        if self._embedding_manager is None and self.enable_search:
            self._embedding_manager = get_embedding_manager()
        return self._embedding_manager
    
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
    
    async def get_session_by_id(self, session_id: Union[str, uuid.UUID]) -> Optional[ConversationSession]:
        """Get session by ID
        
        Args:
            session_id: Session identifier
            
        Returns:
            Optional[ConversationSession]: Session if exists
        """
        async with await get_db_session() as session:
            stmt = select(ConversationSession).where(ConversationSession.id == session_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
    
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
    
    async def add_message(self, session_id: Union[str, uuid.UUID], role: str, content: str, 
                         metadata: Optional[Dict[str, Any]] = None) -> ConversationMessage:
        """Add message to conversation session
        
        Args:
            session_id: Session identifier
            role: Message role ('user' or 'assistant')
            content: Message content
            metadata: Optional metadata
            
        Returns:
            ConversationMessage: Created message
        """
        # Generate embedding for the message (skip for very long content or function results)
        embedding_data = None
        if self.enable_search:
            try:
                # Skip embedding for very long content (>1000 chars) or function call results
                should_skip_embedding = (
                    len(content) > 1000 or 
                    (metadata and metadata.get('function_call_data')) or
                    'RunResult:' in content  # Web search results
                )
                
                if not should_skip_embedding and self.embedding_manager:
                    embedding = await self.embedding_manager.generate_embedding(content)
                    if embedding:
                        # Store as JSON list for SQLite compatibility
                        embedding_data = embedding
            except Exception as e:
                print(f"[Repository] Embedding generation failed, skipping: {e}")
                embedding_data = None
        
        async with await get_db_session() as session:
            message = ConversationMessage(
                session_id=session_id,
                role=role,
                content=content,
                # embedding removed - using Qdrant for vector search instead
                message_metadata=metadata or {},
                token_count=len(content.split())  # Simple token estimation
            )
            
            session.add(message)
            
            # Update session message count and last activity
            await session.execute(
                update(ConversationSession).where(
                    ConversationSession.id == session_id
                ).values(
                    message_count=ConversationSession.message_count + 1,
                    last_activity=datetime.utcnow()
                )
            )
            
            await session.commit()
            await session.refresh(message)
            return message
    
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
            
            # Delete messages not in keep list
            delete_stmt = delete(ConversationMessage).where(
                and_(
                    ConversationMessage.session_id == session_id,
                    ~ConversationMessage.id.in_(keep_ids)
                )
            )
            
            result = await session.execute(delete_stmt)
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
        # Generate embedding for summary
        embedding_data = None
        if self.enable_search:
            try:
                if len(summary) <= 2000 and self.embedding_manager:  # Only generate embeddings for reasonable-length summaries
                    embedding = await self.embedding_manager.generate_embedding(summary)
                    if embedding:
                        # Store as JSON string for SQLite compatibility
                        embedding_data = self.embedding_manager.serialize_embedding(embedding)
            except Exception as e:
                print(f"[Repository] Summary embedding generation failed, skipping: {e}")
                embedding_data = None
        
        async with await get_db_session() as session:
            archive = ConversationArchive(
                user_id=user_id,
                character_name=character_name,
                original_session_id=original_session_id,
                summary=summary,
                summary_embedding=embedding_data,
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
        async with await get_db_session() as session:
            stmt = select(ConversationArchive).where(
                and_(
                    ConversationArchive.user_id == user_id,
                    ConversationArchive.character_name == character_name
                )
            ).order_by(desc(ConversationArchive.archived_at))
            
            result = await session.execute(stmt)
            archives = result.scalars().all()
            
            # Calculate similarities and filter
            results = []
            for archive in archives:
                if archive.summary_embedding:
                    archive_embedding = self.embedding_manager.deserialize_embedding(archive.summary_embedding)
                    if archive_embedding:
                        similarity = self.embedding_manager.calculate_similarity(query_embedding, archive_embedding)
                        if similarity >= similarity_threshold:
                            results.append((archive, similarity))
            
            # Sort by similarity and limit
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]
    
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
