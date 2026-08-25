"""
Repository layer for conversation memory data access
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple, Union
from sqlalchemy import case, select, delete, update, func, desc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db_session
from .models import ConversationSession, ConversationMessage, ConversationArchive, ConversationHistory
logger = logging.getLogger(__name__)


def _monotonic_activity(value):
    """Advance activity without allowing an older concurrent writer to regress it."""
    return case(
        (ConversationSession.last_activity.is_(None), value),
        (ConversationSession.last_activity < value, value),
        else_=ConversationSession.last_activity,
    )


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
            ).order_by(
                desc(
                    func.coalesce(
                        ConversationSession.last_activity,
                        ConversationSession.session_start,
                    )
                ).nullslast(),
                desc(ConversationSession.session_start).nullslast(),
                desc(ConversationSession.id),
            )

            result = await session.execute(stmt)
            sessions = list(result.scalars().all())
            if not sessions:
                return None

            if len(sessions) > 1:
                extra_ids = [session_row.id for session_row in sessions[1:]]
                await session.execute(
                    update(ConversationSession).where(
                        ConversationSession.id.in_(extra_ids)
                    ).values(is_active=False)
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
        expected_title: Optional[str] = None,
    ) -> bool:
        """Update the title for a conversation session."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            # A generated title is presentation metadata, not a new chat
            # activity. Keeping last_activity unchanged prevents a title job
            # racing with mark-as-read from creating a false unread marker.
            values = {"title": title}
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
            if expected_title is not None:
                stmt = stmt.where(ConversationSession.title == expected_title)
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
            ).values(last_activity=_monotonic_activity(datetime.utcnow()))
            
            await session.execute(stmt)
            await session.commit()
    
    async def deactivate_session(
        self,
        session_id: Union[str, uuid.UUID],
        touch_activity: bool = True,
    ):
        """Deactivate a session (mark as inactive)
        
        Args:
            session_id: Session identifier
        """
        # Convert string to UUID if needed
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)
            
        async with await get_db_session() as session:
            values = {"is_active": False}
            if touch_activity:
                values["last_activity"] = _monotonic_activity(datetime.utcnow())
            stmt = update(ConversationSession).where(
                ConversationSession.id == session_id
            ).values(**values)
            
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
        message_id: Optional[str] = None,
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
            conversation_result = await session.execute(
                select(ConversationSession)
                .where(ConversationSession.id == session_id)
                .with_for_update()
            )
            conversation = conversation_result.scalar_one_or_none()
            if conversation is None:
                raise ValueError(f"Conversation session not found: {session_id}")
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

            message_identity = {
                "id": uuid.UUID(message_id)
            } if message_id else {}
            message = ConversationMessage(
                **message_identity,
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
            
            # Update session message count and durable chat status. Only
            # user/assistant turns are activity events; system/maintenance
            # rows must not move the sidebar history.
            update_values = {
                "message_count": func.coalesce(
                    ConversationSession.message_count, 0
                )
                + 1,
            }
            if role in {"user", "assistant"}:
                update_values["last_activity"] = _monotonic_activity(datetime.utcnow())
                update_values["development_status"] = (
                    "working" if role == "user" else "waiting_for_user"
                )
            await session.execute(
                update(ConversationSession).where(
                    ConversationSession.id == session_id
                ).values(**update_values)
            )
            
            await session.commit()
            await session.refresh(message)
            return message

    async def _archive_current_summary_generation(
        self,
        session: AsyncSession,
        conversation: ConversationSession,
        superseded_at: datetime,
    ) -> None:
        """Preserve a current summary unless its latest archive already matches."""
        previous_summary = str(conversation.current_summary or "").strip()
        if not previous_summary:
            return

        latest_result = await session.execute(
            select(ConversationArchive)
            .where(
                ConversationArchive.original_session_id == str(conversation.id)
            )
            .order_by(
                desc(ConversationArchive.archived_at),
                desc(ConversationArchive.id),
            )
            .limit(1)
        )
        latest_archive = latest_result.scalar_one_or_none()
        if (
            latest_archive is not None
            and str(latest_archive.summary or "").strip() == previous_summary
        ):
            return

        session.add(
            ConversationArchive(
                user_id=conversation.user_id,
                character_name=conversation.character_name,
                original_session_id=str(conversation.id),
                summary=previous_summary,
                message_count=int(conversation.message_count or 0),
                start_time=conversation.session_start,
                end_time=conversation.last_activity or superseded_at,
                message_metadata={
                    "archive_type": "summary_revision",
                    "superseded_at": superseded_at.isoformat(),
                },
            )
        )

    async def update_session_summary(
        self,
        session_id: Union[str, uuid.UUID],
        summary: str,
        expected_previous_summary: Optional[str] = None,
    ) -> bool:
        """Update the current summary while preserving the previous generation."""
        if isinstance(session_id, str):
            session_id = uuid.UUID(session_id)

        async with await get_db_session() as session:
            try:
                result = await session.execute(
                    select(ConversationSession)
                    .where(ConversationSession.id == session_id)
                    .with_for_update()
                )
                conversation = result.scalar_one_or_none()
                if not conversation:
                    return False

                previous_summary = str(conversation.current_summary or "").strip()
                if (
                    expected_previous_summary is not None
                    and previous_summary
                    != str(expected_previous_summary or "").strip()
                ):
                    return False
                next_summary = str(summary or "").strip()
                if not next_summary:
                    return False

                now = datetime.utcnow()
                if previous_summary and previous_summary != next_summary:
                    await self._archive_current_summary_generation(
                        session,
                        conversation,
                        now,
                    )

                conversation.current_summary = next_summary
                await session.commit()
                return True
            except Exception:
                await session.rollback()
                raise

    async def update_session_context(
        self,
        session_id: Union[str, uuid.UUID],
        context: Dict[str, Any],
    ) -> bool:
        """Merge non-display runtime state into a conversation session."""
        session_uuid = uuid.UUID(str(session_id)) if isinstance(session_id, str) else session_id
        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_uuid)
            if not conversation:
                return False
            existing = conversation.context if isinstance(conversation.context, dict) else {}
            conversation.context = {**existing, **(context or {})}
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

    async def delete_message_by_id(
        self,
        session_id: Union[str, uuid.UUID],
        message_id: Union[str, uuid.UUID],
    ) -> bool:
        """Delete one uncommitted chat row while preserving branch links."""
        session_uuid = (
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id
        )
        message_uuid = (
            uuid.UUID(message_id) if isinstance(message_id, str) else message_id
        )
        async with await get_db_session() as session:
            conversation = await session.scalar(
                select(ConversationSession)
                .where(ConversationSession.id == session_uuid)
                .with_for_update()
            )
            if conversation is None:
                return False
            message = await session.scalar(
                select(ConversationMessage)
                .where(
                    ConversationMessage.id == message_uuid,
                    ConversationMessage.session_id == session_uuid,
                )
                .with_for_update()
            )
            if message is None:
                return False

            await session.execute(
                update(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.parent_message_id == message_uuid,
                )
                .values(parent_message_id=message.parent_message_id)
            )
            await session.delete(message)
            await session.flush()
            remaining_count = await session.scalar(
                select(func.count()).where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.deleted_at.is_(None),
                )
            )
            conversation.message_count = int(remaining_count or 0)
            latest_message = await self._latest_active_message(session, session_uuid)
            conversation.development_status = (
                "working"
                if latest_message is not None and latest_message.role == "user"
                else (
                    "waiting_for_user"
                    if latest_message is not None
                    and latest_message.role == "assistant"
                    else None
                )
            )
            await session.commit()
            return True

    async def update_message_metadata(
        self,
        session_id: Union[str, uuid.UUID],
        message_id: Union[str, uuid.UUID],
        updates: Dict[str, Any],
    ) -> bool:
        """Merge durable receipt fields into one conversation message."""
        session_uuid = (
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id
        )
        message_uuid = (
            uuid.UUID(message_id) if isinstance(message_id, str) else message_id
        )
        async with await get_db_session() as session:
            message = await session.scalar(
                select(ConversationMessage)
                .where(
                    ConversationMessage.id == message_uuid,
                    ConversationMessage.session_id == session_uuid,
                )
                .with_for_update()
            )
            if message is None:
                return False
            message.message_metadata = {
                **(message.message_metadata or {}),
                **dict(updates or {}),
            }
            await session.commit()
            return True

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
                .values(message_count=len(keep_ids))
            )
            await session.commit()

            return result.rowcount

    async def delete_messages_by_ids(
        self,
        session_id: Union[str, uuid.UUID],
        message_ids: List[Union[str, uuid.UUID]],
    ) -> int:
        """Delete only a previously snapshotted prefix.

        This is used by asynchronous summarization so messages appended while
        the summary was being generated are never deleted accidentally.
        """
        if not message_ids:
            return 0
        session_uuid = uuid.UUID(str(session_id)) if isinstance(session_id, str) else session_id
        ids = [uuid.UUID(str(message_id)) if isinstance(message_id, str) else message_id for message_id in message_ids]
        async with await get_db_session() as session:
            active_result = await session.execute(
                select(ConversationMessage.id).where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.is_active_branch == True,
                    ConversationMessage.id.in_(ids),
                )
            )
            active_ids = {row[0] for row in active_result.fetchall()}
            if active_ids != set(ids):
                # The branch changed while summarization was running.  Never
                # delete rows that are no longer part of the active branch.
                return 0
            await session.execute(
                update(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.is_active_branch == True,
                    ConversationMessage.parent_message_id.in_(ids),
                )
                .values(parent_message_id=None, branch_index=0)
            )
            result = await session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.is_active_branch == True,
                    ConversationMessage.id.in_(ids),
                )
            )
            remaining = await session.execute(
                select(ConversationMessage.id).where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.deleted_at.is_(None),
                )
            )
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == session_uuid)
                .values(message_count=len(remaining.all()))
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def apply_summary_checkpoint(
        self,
        session_id: Union[str, uuid.UUID],
        message_ids: List[Union[str, uuid.UUID]],
        summary: str,
        start_time: datetime,
        end_time: datetime,
        metadata: Optional[Dict[str, Any]] = None,
        expected_previous_summary: Optional[str] = None,
    ) -> Optional[Tuple[ConversationArchive, int]]:
        """Atomically archive, delete, and replace a session summary.

        The session and snapshotted message rows are locked before validation.
        If the active branch changed or any write fails, no part of the
        checkpoint is committed.
        """
        if not message_ids or not str(summary or "").strip():
            return None

        session_uuid = (
            uuid.UUID(str(session_id)) if isinstance(session_id, str) else session_id
        )
        ids = [
            uuid.UUID(str(message_id)) if isinstance(message_id, str) else message_id
            for message_id in message_ids
        ]
        if len(set(ids)) != len(ids):
            return None

        async with await get_db_session() as session:
            try:
                session_result = await session.execute(
                    select(ConversationSession)
                    .where(ConversationSession.id == session_uuid)
                    .with_for_update()
                )
                conversation = session_result.scalar_one_or_none()
                if conversation is None:
                    return None
                if (
                    expected_previous_summary is not None
                    and str(conversation.current_summary or "").strip()
                    != str(expected_previous_summary or "").strip()
                ):
                    return None

                message_result = await session.execute(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == session_uuid,
                        ConversationMessage.is_active_branch == True,
                        ConversationMessage.id.in_(ids),
                    )
                    .with_for_update()
                )
                snapshotted_messages = list(message_result.scalars().all())
                if {message.id for message in snapshotted_messages} != set(ids):
                    return None

                now = datetime.utcnow()
                if (
                    str(conversation.current_summary or "").strip()
                    and str(conversation.current_summary or "").strip()
                    != str(summary).strip()
                ):
                    await self._archive_current_summary_generation(
                        session,
                        conversation,
                        now,
                    )

                archive_metadata = dict(metadata or {})
                archive_metadata["archive_type"] = "summary_checkpoint"
                archive = ConversationArchive(
                    user_id=conversation.user_id,
                    character_name=conversation.character_name,
                    original_session_id=str(conversation.id),
                    summary=str(summary).strip(),
                    message_count=len(ids),
                    start_time=start_time,
                    end_time=end_time,
                    message_metadata=archive_metadata,
                )
                session.add(archive)
                await session.flush()

                await session.execute(
                    update(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == session_uuid,
                        ~ConversationMessage.id.in_(ids),
                        ConversationMessage.parent_message_id.in_(ids),
                    )
                    .values(parent_message_id=None, branch_index=0)
                )
                delete_result = await session.execute(
                    delete(ConversationMessage).where(
                        ConversationMessage.session_id == session_uuid,
                        ConversationMessage.is_active_branch == True,
                        ConversationMessage.id.in_(ids),
                    )
                )
                deleted_count = int(delete_result.rowcount or 0)
                if deleted_count != len(ids):
                    await session.rollback()
                    return None

                remaining_result = await session.execute(
                    select(func.count()).where(
                        ConversationMessage.session_id == session_uuid,
                        ConversationMessage.deleted_at.is_(None),
                    )
                )
                conversation.message_count = int(remaining_result.scalar() or 0)
                conversation.current_summary = str(summary).strip()
                await session.commit()
                return archive, deleted_count
            except Exception:
                await session.rollback()
                raise

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
