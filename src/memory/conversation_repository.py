"""
Conversation Session Repository

Provides CRUD operations for conversation sessions and messages.
Used for managing chat history with PostgreSQL persistence.
"""

import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc, and_
from sqlalchemy.orm import selectinload

from .models import ConversationSession, ConversationMessage
from .database import get_database_manager


class ConversationRepository:
    """Repository for conversation session operations"""
    
    def __init__(self, session: Optional[AsyncSession] = None):
        """Initialize repository
        
        Args:
            session: Optional AsyncSession, if None will get from manager
        """
        self._session = session
    
    async def _get_session(self) -> AsyncSession:
        """Get database session"""
        if self._session:
            return self._session
        db_manager = get_database_manager()
        return await db_manager.get_session()
    
    # ─── Session CRUD ───────────────────────────────────────────────────
    
    async def create_session(
        self,
        user_id: str,
        character_name: str,
        title: str = '',
        project_id: Optional[str] = None
    ) -> ConversationSession:
        """Create a new conversation session
        
        Args:
            user_id: User ID (username or UUID string)
            character_name: Character name for this session
            title: Optional title for the session
            project_id: Optional project ID to associate with this session
            
        Returns:
            Created ConversationSession
        """
        session = await self._get_session()
        try:
            new_session = ConversationSession(
                user_id=user_id,
                character_name=character_name,
                title=title,
                is_active=True,
                message_count=0,
                project_id=uuid.UUID(project_id) if project_id else None
            )
            session.add(new_session)
            await session.commit()
            await session.refresh(new_session)
            return new_session
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def get_session_by_id(
        self,
        session_id: str,
        with_messages: bool = False
    ) -> Optional[ConversationSession]:
        """Get session by ID
        
        Args:
            session_id: Session UUID string
            with_messages: If True, eagerly load messages
            
        Returns:
            ConversationSession or None
        """
        session = await self._get_session()
        try:
            query = select(ConversationSession).where(
                ConversationSession.id == uuid.UUID(session_id)
            )
            if with_messages:
                query = query.options(selectinload(ConversationSession.messages))
            
            result = await session.execute(query)
            return result.scalar_one_or_none()
        finally:
            if not self._session:
                await session.close()
    
    async def get_user_sessions(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        include_inactive: bool = True,
        project_id: Optional[str] = None
    ) -> List[ConversationSession]:
        """Get sessions for a user
        
        Args:
            user_id: User ID
            limit: Max number of sessions to return
            offset: Offset for pagination
            include_inactive: Include inactive sessions
            project_id: Optional filter by project ID
                       - None: all sessions
                       - "": only sessions without project_id (NULL)
                       - "<uuid>": sessions with that specific project_id
            
        Returns:
            List of ConversationSession
        """
        session = await self._get_session()
        try:
            conditions = [
                ConversationSession.user_id == user_id,
                ConversationSession.deleted_at.is_(None)  # ソフトデリートされていないもののみ
            ]
            
            # Handle project_id filtering
            if project_id is not None:
                if project_id == "":
                    # Empty string means: only sessions without project_id
                    conditions.append(ConversationSession.project_id.is_(None))
                else:
                    # Specific project_id
                    conditions.append(ConversationSession.project_id == uuid.UUID(project_id))
            # If project_id is None, don't add any project filter (return all)
            
            query = select(ConversationSession).where(and_(*conditions))
            
            if not include_inactive:
                query = query.where(ConversationSession.is_active == True)
            
            query = query.order_by(desc(ConversationSession.last_activity))
            query = query.limit(limit).offset(offset)
            
            result = await session.execute(query)
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()
    
    async def get_sessions_by_project(
        self,
        project_id: str,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[ConversationSession]:
        """Get sessions for a specific project
        
        Args:
            project_id: Project UUID string
            user_id: Optional filter by user ID
            limit: Max number of sessions to return
            offset: Offset for pagination
            
        Returns:
            List of ConversationSession
        """
        session = await self._get_session()
        try:
            conditions = [
                ConversationSession.project_id == uuid.UUID(project_id),
                ConversationSession.deleted_at.is_(None)
            ]
            
            if user_id:
                conditions.append(ConversationSession.user_id == user_id)
            
            query = select(ConversationSession).where(and_(*conditions))
            query = query.order_by(desc(ConversationSession.last_activity))
            query = query.limit(limit).offset(offset)
            
            result = await session.execute(query)
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()
    
    async def get_active_session(
        self,
        user_id: str,
        character_name: Optional[str] = None
    ) -> Optional[ConversationSession]:
        """Get the most recent active session for a user
        
        Args:
            user_id: User ID
            character_name: Optional filter by character
            
        Returns:
            ConversationSession or None
        """
        session = await self._get_session()
        try:
            conditions = [
                ConversationSession.user_id == user_id,
                ConversationSession.is_active == True
            ]
            if character_name:
                conditions.append(ConversationSession.character_name == character_name)
            
            query = select(ConversationSession).where(
                and_(*conditions)
            ).order_by(desc(ConversationSession.last_activity)).limit(1)
            
            result = await session.execute(query)
            return result.scalar_one_or_none()
        finally:
            if not self._session:
                await session.close()
    
    async def update_session(
        self,
        session_id: str,
        **kwargs
    ) -> bool:
        """Update session fields
        
        Args:
            session_id: Session UUID string
            **kwargs: Fields to update (title, is_active, etc.)
            
        Returns:
            True if updated successfully
        """
        session = await self._get_session()
        try:
            kwargs['last_activity'] = datetime.utcnow()
            
            stmt = update(ConversationSession).where(
                ConversationSession.id == uuid.UUID(session_id)
            ).values(**kwargs)
            
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def update_session_title(
        self,
        session_id: str,
        title: str
    ) -> bool:
        """Update session title
        
        Args:
            session_id: Session UUID string
            title: New title
            
        Returns:
            True if updated
        """
        return await self.update_session(session_id, title=title)
    
    async def deactivate_session(self, session_id: str) -> bool:
        """Mark session as inactive
        
        Args:
            session_id: Session UUID string
            
        Returns:
            True if updated
        """
        return await self.update_session(session_id, is_active=False)
    
    async def delete_session(self, session_id: str) -> bool:
        """Soft delete a session (mark as deleted, actual deletion after 3 months)
        
        Args:
            session_id: Session UUID string
            
        Returns:
            True if marked as deleted
        """
        return await self.update_session(session_id, deleted_at=datetime.utcnow())
    
    async def permanently_delete_old_sessions(self, days: int = 90) -> int:
        """Permanently delete sessions that were soft-deleted more than N days ago
        
        Args:
            days: Number of days after soft deletion to permanently delete (default: 90 = 3 months)
            
        Returns:
            Number of sessions permanently deleted
        """
        from datetime import timedelta
        
        session = await self._get_session()
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            
            stmt = delete(ConversationSession).where(
                and_(
                    ConversationSession.deleted_at.isnot(None),
                    ConversationSession.deleted_at < cutoff_date
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    # ─── Message CRUD ───────────────────────────────────────────────────
    
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        token_count: Optional[int] = None
    ) -> ConversationMessage:
        """Add a message to a session
        
        Args:
            session_id: Session UUID string
            role: Message role ('user' or 'assistant')
            content: Message content
            metadata: Optional message metadata
            token_count: Optional token count
            
        Returns:
            Created ConversationMessage
        """
        session = await self._get_session()
        try:
            message = ConversationMessage(
                session_id=uuid.UUID(session_id),
                role=role,
                content=content,
                message_metadata=metadata or {},
                token_count=token_count
            )
            session.add(message)
            
            # Update session message count and last activity
            stmt = update(ConversationSession).where(
                ConversationSession.id == uuid.UUID(session_id)
            ).values(
                message_count=ConversationSession.message_count + 1,
                last_activity=datetime.utcnow()
            )
            await session.execute(stmt)
            
            await session.commit()
            await session.refresh(message)
            return message
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def get_session_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[ConversationMessage]:
        """Get messages for a session
        
        Args:
            session_id: Session UUID string
            limit: Max messages to return (None for all)
            offset: Offset for pagination
            
        Returns:
            List of ConversationMessage ordered by created_at
        """
        session = await self._get_session()
        try:
            query = select(ConversationMessage).where(
                ConversationMessage.session_id == uuid.UUID(session_id)
            ).order_by(ConversationMessage.created_at)
            
            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)
            
            result = await session.execute(query)
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()
    
    async def get_recent_messages(
        self,
        session_id: str,
        count: int = 20
    ) -> List[ConversationMessage]:
        """Get the most recent messages for a session
        
        Args:
            session_id: Session UUID string
            count: Number of recent messages
            
        Returns:
            List of ConversationMessage (oldest first)
        """
        session = await self._get_session()
        try:
            # Subquery to get latest N message IDs
            subq = select(ConversationMessage.id).where(
                ConversationMessage.session_id == uuid.UUID(session_id)
            ).order_by(desc(ConversationMessage.created_at)).limit(count)
            
            # Main query to get those messages in chronological order
            query = select(ConversationMessage).where(
                ConversationMessage.id.in_(subq)
            ).order_by(ConversationMessage.created_at)
            
            result = await session.execute(query)
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()
    
    async def count_user_sessions(self, user_id: str) -> int:
        """Count total sessions for a user
        
        Args:
            user_id: User ID
            
        Returns:
            Session count
        """
        session = await self._get_session()
        try:
            from sqlalchemy import func
            query = select(func.count()).where(
                ConversationSession.user_id == user_id
            )
            result = await session.execute(query)
            return result.scalar() or 0
        finally:
            if not self._session:
                await session.close()
    
    # ─── Branching Operations ─────────────────────────────────────────────
    
    async def add_message_with_parent(
        self,
        session_id: str,
        role: str,
        content: str,
        parent_message_id: Optional[str] = None,
        branch_index: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        token_count: Optional[int] = None
    ) -> ConversationMessage:
        """Add a message with parent linkage for branching support
        
        Args:
            session_id: Session UUID string
            role: Message role ('user' or 'assistant')
            content: Message content
            parent_message_id: Optional parent message ID
            branch_index: Branch index among siblings
            metadata: Optional message metadata
            token_count: Optional token count
            
        Returns:
            Created ConversationMessage
        """
        session = await self._get_session()
        try:
            message = ConversationMessage(
                session_id=uuid.UUID(session_id),
                role=role,
                content=content,
                parent_message_id=uuid.UUID(parent_message_id) if parent_message_id else None,
                branch_index=branch_index,
                is_active_branch=True,
                message_metadata=metadata or {},
                token_count=token_count
            )
            session.add(message)
            
            # Update session message count and last activity
            stmt = update(ConversationSession).where(
                ConversationSession.id == uuid.UUID(session_id)
            ).values(
                message_count=ConversationSession.message_count + 1,
                last_activity=datetime.utcnow()
            )
            await session.execute(stmt)
            
            await session.commit()
            await session.refresh(message)
            return message
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def edit_message_and_branch(
        self,
        message_id: str,
        new_content: str
    ) -> ConversationMessage:
        """Edit a message by creating a new branch
        
        This deactivates the original message's branch and creates a new branch
        with the edited content. Following messages in the original branch are
        also deactivated.
        
        Args:
            message_id: ID of the message to edit
            new_content: New message content
            
        Returns:
            New ConversationMessage with edited content
        """
        session = await self._get_session()
        try:
            # Get the original message
            query = select(ConversationMessage).where(
                ConversationMessage.id == uuid.UUID(message_id)
            )
            result = await session.execute(query)
            original_msg = result.scalar_one_or_none()
            
            if not original_msg:
                raise ValueError(f"Message not found: {message_id}")
            
            # Deactivate the original message and all following messages in the same branch
            await self._deactivate_branch_from_message(session, message_id)
            
            # Count existing siblings to get new branch_index
            sibling_count = await self._count_branch_siblings(
                session, 
                str(original_msg.session_id),
                str(original_msg.parent_message_id) if original_msg.parent_message_id else None
            )
            
            # Create new message with same parent but new branch_index
            new_message = ConversationMessage(
                session_id=original_msg.session_id,
                role=original_msg.role,
                content=new_content,
                parent_message_id=original_msg.parent_message_id,
                branch_index=sibling_count,  # New branch
                is_active_branch=True,
                message_metadata=original_msg.message_metadata or {},
                token_count=None  # Will be recalculated
            )
            session.add(new_message)
            await session.commit()
            await session.refresh(new_message)
            return new_message
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def _deactivate_branch_from_message(
        self,
        session: AsyncSession,
        message_id: str
    ):
        """Deactivate a message and all following messages in the same branch"""
        # Mark the message as inactive
        stmt = update(ConversationMessage).where(
            ConversationMessage.id == uuid.UUID(message_id)
        ).values(is_active_branch=False)
        await session.execute(stmt)
        
        # Find and deactivate all child messages recursively
        # Get all messages that have this message as parent
        query = select(ConversationMessage).where(
            and_(
                ConversationMessage.parent_message_id == uuid.UUID(message_id),
                ConversationMessage.is_active_branch == True
            )
        )
        result = await session.execute(query)
        children = list(result.scalars().all())
        
        for child in children:
            await self._deactivate_branch_from_message(session, str(child.id))
    
    async def _count_branch_siblings(
        self,
        session: AsyncSession,
        session_id: str,
        parent_message_id: Optional[str]
    ) -> int:
        """Count number of sibling branches (messages with same parent)"""
        from sqlalchemy import func
        
        conditions = [ConversationMessage.session_id == uuid.UUID(session_id)]
        
        if parent_message_id:
            conditions.append(
                ConversationMessage.parent_message_id == uuid.UUID(parent_message_id)
            )
        else:
            conditions.append(ConversationMessage.parent_message_id.is_(None))
        
        query = select(func.count()).where(and_(*conditions))
        result = await session.execute(query)
        return result.scalar() or 0
    
    async def get_branch_siblings(
        self,
        message_id: str
    ) -> List[Dict[str, Any]]:
        """Get all sibling branches for a message (including itself)
        
        Args:
            message_id: Message ID
            
        Returns:
            List of sibling message dicts with branch info
        """
        session = await self._get_session()
        try:
            # Get the message to find its parent
            query = select(ConversationMessage).where(
                ConversationMessage.id == uuid.UUID(message_id)
            )
            result = await session.execute(query)
            message = result.scalar_one_or_none()
            
            if not message:
                return []
            
            # Find all siblings (same parent_message_id)
            if message.parent_message_id:
                sibling_query = select(ConversationMessage).where(
                    ConversationMessage.parent_message_id == message.parent_message_id
                ).order_by(ConversationMessage.branch_index)
            else:
                # Root messages - find all with null parent in this session
                sibling_query = select(ConversationMessage).where(
                    and_(
                        ConversationMessage.session_id == message.session_id,
                        ConversationMessage.parent_message_id.is_(None),
                        ConversationMessage.role == message.role
                    )
                ).order_by(ConversationMessage.branch_index)
            
            result = await session.execute(sibling_query)
            siblings = list(result.scalars().all())
            
            return [
                {
                    'id': str(s.id),
                    'content': s.content,
                    'branch_index': s.branch_index,
                    'is_active_branch': s.is_active_branch,
                    'created_at': s.created_at.isoformat() if s.created_at else None
                }
                for s in siblings
            ]
        finally:
            if not self._session:
                await session.close()
    
    async def switch_active_branch(
        self,
        session_id: str,
        target_message_id: str
    ) -> bool:
        """Switch to a different branch by activating a message and its descendants
        
        Args:
            session_id: Session UUID string
            target_message_id: Message ID to switch to
            
        Returns:
            True if switched successfully
        """
        session = await self._get_session()
        try:
            # Get the target message
            query = select(ConversationMessage).where(
                ConversationMessage.id == uuid.UUID(target_message_id)
            )
            result = await session.execute(query)
            target_msg = result.scalar_one_or_none()
            
            if not target_msg:
                return False
            
            # Deactivate all siblings
            if target_msg.parent_message_id:
                sibling_query = update(ConversationMessage).where(
                    ConversationMessage.parent_message_id == target_msg.parent_message_id
                ).values(is_active_branch=False)
            else:
                # Root level - deactivate all root messages of same role
                sibling_query = update(ConversationMessage).where(
                    and_(
                        ConversationMessage.session_id == uuid.UUID(session_id),
                        ConversationMessage.parent_message_id.is_(None),
                        ConversationMessage.role == target_msg.role
                    )
                ).values(is_active_branch=False)
            
            await session.execute(sibling_query)
            
            # Activate the target message
            activate_stmt = update(ConversationMessage).where(
                ConversationMessage.id == uuid.UUID(target_message_id)
            ).values(is_active_branch=True)
            await session.execute(activate_stmt)
            
            # Activate descendants in the target branch
            await self._activate_branch_descendants(session, target_message_id)
            
            await session.commit()
            return True
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()
    
    async def _activate_branch_descendants(
        self,
        session: AsyncSession,
        message_id: str
    ):
        """Recursively activate the first child branch of a message"""
        # Find children of this message
        query = select(ConversationMessage).where(
            ConversationMessage.parent_message_id == uuid.UUID(message_id)
        ).order_by(ConversationMessage.branch_index)
        
        result = await session.execute(query)
        children = list(result.scalars().all())
        
        if not children:
            return
        
        # Activate the first child (or the one with lowest branch_index)
        first_child = children[0]
        stmt = update(ConversationMessage).where(
            ConversationMessage.id == first_child.id
        ).values(is_active_branch=True)
        await session.execute(stmt)
        
        # Recursively activate its descendants
        await self._activate_branch_descendants(session, str(first_child.id))
    
    async def get_active_branch_messages(
        self,
        session_id: str
    ) -> List[ConversationMessage]:
        """Get only messages in the active branch
        
        Args:
            session_id: Session UUID string
            
        Returns:
            List of ConversationMessage in active branch, ordered by created_at
        """
        session = await self._get_session()
        try:
            query = select(ConversationMessage).where(
                and_(
                    ConversationMessage.session_id == uuid.UUID(session_id),
                    ConversationMessage.is_active_branch == True
                )
            ).order_by(ConversationMessage.created_at)
            
            result = await session.execute(query)
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()
    
    async def get_message_by_id(
        self,
        message_id: str
    ) -> Optional[ConversationMessage]:
        """Get a single message by ID
        
        Args:
            message_id: Message UUID string
            
        Returns:
            ConversationMessage or None
        """
        session = await self._get_session()
        try:
            query = select(ConversationMessage).where(
                ConversationMessage.id == uuid.UUID(message_id)
            )
            result = await session.execute(query)
            return result.scalar_one_or_none()
        finally:
            if not self._session:
                await session.close()


# Convenience function to get repository instance
def get_conversation_repository() -> ConversationRepository:
    """Get a ConversationRepository instance"""
    return ConversationRepository()
