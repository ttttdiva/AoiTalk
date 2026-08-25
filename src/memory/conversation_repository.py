"""
Conversation Session Repository

Provides CRUD operations for conversation sessions and messages.
Used for managing chat history with PostgreSQL persistence.
"""

import copy
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, select, update, delete, desc, and_, or_, func
from sqlalchemy.orm import selectinload

from .models import ConversationSession, ConversationMessage, ConversationParticipant
from .database import get_database_manager


def _monotonic_activity(value):
    """Advance activity without allowing an older concurrent writer to regress it."""
    return case(
        (ConversationSession.last_activity.is_(None), value),
        (ConversationSession.last_activity < value, value),
        else_=ConversationSession.last_activity,
    )


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
        user_id: Union[str, List[str]],
        character_name: str,
        title: str = '',
        project_id: Optional[str] = None,
        app_id: Optional[str] = None,
        app_target_id: Optional[str] = None,
        development_status: Optional[str] = None,
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
                project_id=uuid.UUID(project_id) if project_id else None,
                app_id=uuid.UUID(app_id) if app_id else None,
                app_target_id=uuid.UUID(app_target_id) if app_target_id else None,
                development_status=development_status if app_id else None,
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

    async def fork_session(
        self,
        source_session_id: str,
        source_message_id: str,
        *,
        user_id: str,
        title: str | None = None,
    ) -> ConversationSession:
        """指定メッセージまでの経路を独立した会話セッションへ複製する。"""

        session = await self._get_session()
        try:
            source_uuid = uuid.UUID(source_session_id)
            message_uuid = uuid.UUID(source_message_id)
            source = await session.scalar(
                select(ConversationSession)
                .options(selectinload(ConversationSession.participants))
                .where(
                    ConversationSession.id == source_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if source is None:
                raise ValueError("Source session not found")

            target = await session.get(ConversationMessage, message_uuid)
            if target is None or target.session_id != source_uuid:
                raise ValueError("Fork message does not belong to the source session")

            path: list[ConversationMessage] = []
            cursor: ConversationMessage | None = target
            visited: set[uuid.UUID] = set()
            while cursor is not None:
                if cursor.id in visited or cursor.session_id != source_uuid:
                    raise ValueError("Conversation branch is invalid")
                visited.add(cursor.id)
                path.append(cursor)
                cursor = (
                    await session.get(ConversationMessage, cursor.parent_message_id)
                    if cursor.parent_message_id
                    else None
                )
            path.reverse()

            inherited_title = str(source.title or "新しい会話").strip()
            fork_title = (title or f"{inherited_title}（フォーク）").strip()[:200]
            inherited_context = copy.deepcopy(source.context or {})
            inherited_context.pop("llm_provider_state", None)
            # A fork has a new active branch and must not inherit a provider
            # process continuation handle from the source conversation.
            inherited_context.pop("cli_native_sessions", None)
            forked = ConversationSession(
                id=uuid.uuid4(),
                user_id=user_id,
                character_name=source.character_name,
                title=fork_title,
                message_count=len(path),
                context=inherited_context,
                # summary はfork地点より後の内容を含み得るため、祖先履歴から再生成する。
                current_summary="",
                is_active=True,
                project_id=source.project_id,
                app_id=source.app_id,
                app_target_id=source.app_target_id,
                parent_session_id=source.id,
                forked_from_message_id=target.id,
                is_group_chat=bool(source.is_group_chat),
                group_character_names=copy.deepcopy(source.group_character_names or []),
                rp_settings=copy.deepcopy(source.rp_settings or {}),
            )
            session.add(forked)
            await session.flush()

            has_owner = False
            for participant in source.participants or []:
                if (
                    participant.participant_type == "user"
                    and str(participant.participant_id) != user_id
                ):
                    continue
                session.add(
                    ConversationParticipant(
                        session_id=forked.id,
                        participant_type=participant.participant_type,
                        participant_id=(
                            user_id
                            if participant.participant_type == "user"
                            else participant.participant_id
                        ),
                        display_name=participant.display_name,
                        role=(
                            "owner"
                            if participant.participant_type == "user"
                            else participant.role
                        ),
                        status="joined",
                        auto_respond=bool(participant.auto_respond),
                        participant_metadata=copy.deepcopy(
                            participant.participant_metadata or {}
                        ),
                    )
                )
                has_owner = has_owner or participant.participant_type == "user"

            if not has_owner:
                session.add(
                    ConversationParticipant(
                        session_id=forked.id,
                        participant_type="user",
                        participant_id=user_id,
                        display_name="",
                        role="owner",
                        status="joined",
                        auto_respond=False,
                        participant_metadata={},
                    )
                )

            copied_ids: dict[uuid.UUID, uuid.UUID] = {}
            for source_message in path:
                new_id = uuid.uuid4()
                copied_ids[source_message.id] = new_id
                metadata = copy.deepcopy(source_message.message_metadata or {})
                metadata.pop("agent_run_id", None)
                metadata.pop("context_snapshot", None)
                metadata["fork_source_message_id"] = str(source_message.id)
                session.add(
                    ConversationMessage(
                        id=new_id,
                        session_id=forked.id,
                        role=source_message.role,
                        content=source_message.content,
                        message_metadata=metadata,
                        sender_type=source_message.sender_type,
                        sender_id=source_message.sender_id,
                        sender_display_name=source_message.sender_display_name,
                        created_at=source_message.created_at,
                        token_count=source_message.token_count,
                        parent_message_id=(
                            copied_ids.get(source_message.parent_message_id)
                            if source_message.parent_message_id
                            else None
                        ),
                        branch_index=0,
                        is_active_branch=True,
                    )
                )

            await session.commit()
            await session.refresh(forked)
            return forked
        except Exception:
            await session.rollback()
            raise
        finally:
            if not self._session:
                await session.close()
    
    async def get_session_by_id(
        self,
        session_id: str,
        with_messages: bool = False,
        include_deleted: bool = False,
        for_update: bool = False,
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
            conditions = [ConversationSession.id == uuid.UUID(session_id)]
            if not include_deleted:
                conditions.append(ConversationSession.deleted_at.is_(None))

            query = select(ConversationSession).where(and_(*conditions))
            options = [selectinload(ConversationSession.participants)]
            if with_messages:
                options.append(selectinload(ConversationSession.messages))
            query = query.options(*options)
            if for_update:
                query = query.with_for_update()
            
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
        project_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[ConversationSession]:
        """Get sessions for a user
        
        Args:
            user_id: User ID or list of visible user IDs
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
            user_ids = [user_id] if isinstance(user_id, str) else list(user_id)
            participant_session_ids = (
                select(ConversationParticipant.session_id)
                .where(
                    and_(
                        ConversationParticipant.participant_type == "user",
                        ConversationParticipant.participant_id.in_(user_ids),
                        ConversationParticipant.status == "joined",
                    )
                )
                .scalar_subquery()
            )
            conditions = [
                or_(
                    ConversationSession.user_id.in_(user_ids),
                    ConversationSession.id.in_(participant_session_ids),
                ),
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

            if app_id is not None:
                conditions.append(ConversationSession.app_id == uuid.UUID(app_id))
            
            query = select(ConversationSession).where(and_(*conditions)).options(
                selectinload(ConversationSession.participants)
            )
            
            if not include_inactive:
                query = query.where(ConversationSession.is_active == True)
            
            # Keep the Python API ordering identical to the frontend BFF:
            # durable activity, then session start, then a stable id tie-break.
            activity_order = desc(
                func.coalesce(
                    ConversationSession.last_activity,
                    ConversationSession.session_start,
                )
            ).nullslast()
            query = query.order_by(
                activity_order,
                desc(ConversationSession.session_start).nullslast(),
                desc(ConversationSession.id),
            )
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
            
            query = select(ConversationSession).where(and_(*conditions)).options(
                selectinload(ConversationSession.participants)
            )
            query = query.order_by(
                desc(
                    func.coalesce(
                        ConversationSession.last_activity,
                        ConversationSession.session_start,
                    )
                ).nullslast(),
                desc(ConversationSession.session_start).nullslast(),
                desc(ConversationSession.id),
            )
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
                ConversationSession.is_active == True,
                ConversationSession.deleted_at.is_(None),
            ]
            if character_name:
                conditions.append(ConversationSession.character_name == character_name)
            
            query = select(ConversationSession).where(
                and_(*conditions)
            ).order_by(
                desc(
                    func.coalesce(
                        ConversationSession.last_activity,
                        ConversationSession.session_start,
                    )
                ).nullslast(),
                desc(ConversationSession.session_start).nullslast(),
                desc(ConversationSession.id),
            ).limit(1)
            
            result = await session.execute(query)
            return result.scalar_one_or_none()
        finally:
            if not self._session:
                await session.close()
    
    async def update_session(
        self,
        session_id: str,
        touch_activity: bool = True,
        expected_character_name: Optional[str] = None,
        expected_title: Optional[str] = None,
        **kwargs
    ) -> bool:
        """Update session fields
        
        Args:
            session_id: Session UUID string
            expected_character_name: Optional optimistic-concurrency guard. If
                set, the update is applied only while the row still contains
                this character name.
            expected_title: Optional optimistic-concurrency guard for generated
                title updates.
            **kwargs: Fields to update (title, is_active, etc.)
            
        Returns:
            True if updated successfully
        """
        session = await self._get_session()
        try:
            if touch_activity:
                kwargs['last_activity'] = datetime.utcnow()
            if kwargs.get("last_activity") is not None:
                kwargs["last_activity"] = _monotonic_activity(
                    kwargs["last_activity"]
                )
            
            conditions = [ConversationSession.id == uuid.UUID(session_id)]
            if "deleted_at" not in kwargs:
                conditions.append(ConversationSession.deleted_at.is_(None))
            if expected_character_name is not None:
                conditions.append(
                    ConversationSession.character_name == expected_character_name
                )
            if expected_title is not None:
                conditions.append(ConversationSession.title == expected_title)

            stmt = update(ConversationSession).where(and_(*conditions)).values(**kwargs)
            
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
        title: str,
        source: Optional[str] = None,
        expected_title: Optional[str] = None,
    ) -> bool:
        """Update session title
        
        Args:
            session_id: Session UUID string
            title: New title
            
        Returns:
            True if updated
        """
        updates: Dict[str, Any] = {"title": title}
        if source:
            existing = await self.get_session_by_id(session_id, with_messages=False)
            current_context = getattr(existing, "context", None) if existing else None
            context = (
                dict(current_context) if isinstance(current_context, dict) else {}
            )
            context["title_generation"] = {"source": source}
            updates["context"] = context
        return await self.update_session(
            session_id,
            touch_activity=False,
            expected_title=expected_title,
            **updates,
        )
    
    async def deactivate_session(
        self,
        session_id: str,
        touch_activity: bool = True,
    ) -> bool:
        """Mark session as inactive
        
        Args:
            session_id: Session UUID string
            
        Returns:
            True if updated
        """
        return await self.update_session(
            session_id,
            is_active=False,
            touch_activity=touch_activity,
        )
    
    async def delete_session(self, session_id: str) -> bool:
        """Soft delete a session (mark as deleted, actual deletion after 3 months)
        
        Args:
            session_id: Session UUID string
            
        Returns:
            True if marked as deleted
        """
        # Soft deletion is lifecycle metadata; do not move a session's
        # activity marker immediately before it leaves the history list.
        return await self.update_session(
            session_id,
            touch_activity=False,
            deleted_at=datetime.utcnow(),
        )
    
    async def permanently_delete_old_sessions(self, days: int | None = None) -> int:
        """Permanently delete sessions past the shared deletion retention.
        
        Args:
            days: Number of days after soft deletion.  When omitted, the
                shared ``AOITALK_DELETION_RETENTION_DAYS`` policy is used.
            
        Returns:
            Number of sessions permanently deleted
        """
        from datetime import timedelta
        if days is None:
            from ..services.content_deletion_service import get_deletion_retention_days

            days = get_deletion_retention_days()
        if days <= 0:
            from ..services.content_deletion_service import DEFAULT_DELETION_RETENTION_DAYS

            days = DEFAULT_DELETION_RETENTION_DAYS
        
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
    
    # ─── Participant CRUD ────────────────────────────────────────────────

    async def ensure_participant(
        self,
        session_id: str,
        participant_type: str,
        participant_id: str,
        *,
        display_name: str = "",
        role: str = "member",
        status: str = "joined",
        auto_respond: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationParticipant:
        """Create or update one conversation participant."""
        session = await self._get_session()
        try:
            uid = uuid.UUID(session_id)
            result = await session.execute(
                select(ConversationParticipant).where(
                    and_(
                        ConversationParticipant.session_id == uid,
                        ConversationParticipant.participant_type == participant_type,
                        ConversationParticipant.participant_id == participant_id,
                    )
                )
            )
            participant = result.scalar_one_or_none()
            if participant:
                participant.display_name = display_name or participant.display_name
                participant.role = role or participant.role
                participant.status = status or participant.status
                participant.auto_respond = auto_respond
                participant.participant_metadata = metadata or participant.participant_metadata or {}
            else:
                participant = ConversationParticipant(
                    session_id=uid,
                    participant_type=participant_type,
                    participant_id=participant_id,
                    display_name=display_name,
                    role=role,
                    status=status,
                    auto_respond=auto_respond,
                    participant_metadata=metadata or {},
                )
                session.add(participant)
            await session.commit()
            await session.refresh(participant)
            return participant
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            if not self._session:
                await session.close()

    async def get_session_participants(
        self, session_id: str
    ) -> List[ConversationParticipant]:
        session = await self._get_session()
        try:
            result = await session.execute(
                select(ConversationParticipant)
                .where(ConversationParticipant.session_id == uuid.UUID(session_id))
                .order_by(ConversationParticipant.created_at, ConversationParticipant.id)
            )
            return list(result.scalars().all())
        finally:
            if not self._session:
                await session.close()

    async def user_has_session_access(self, session_id: str, user_id: str) -> bool:
        session = await self._get_session()
        try:
            result = await session.execute(
                select(ConversationSession.id, ConversationSession.project_id)
                .where(
                    and_(
                        ConversationSession.id == uuid.UUID(session_id),
                        ConversationSession.deleted_at.is_(None),
                        or_(
                            ConversationSession.user_id == user_id,
                            ConversationSession.id.in_(
                                select(ConversationParticipant.session_id).where(
                                    and_(
                                        ConversationParticipant.session_id
                                        == uuid.UUID(session_id),
                                        ConversationParticipant.participant_type
                                        == "user",
                                        ConversationParticipant.participant_id == user_id,
                                        ConversationParticipant.status == "joined",
                                    )
                                )
                            ),
                        ),
                    )
                )
                .limit(1)
            )
            row = result.first()
            if row is None:
                return False
            project_id = row[1]
            if project_id is None:
                return True
            try:
                user_uuid = uuid.UUID(str(user_id))
            except (TypeError, ValueError):
                return False
            from .project_repository import ProjectRepository

            return await ProjectRepository.has_permission(
                session, project_id, user_uuid, "read"
            )
        finally:
            if not self._session:
                await session.close()

    async def user_has_session_write_access(self, session_id: str, user_id: str) -> bool:
        """Return whether a user may generate or mutate a conversation."""
        session = await self._get_session()
        try:
            result = await session.execute(
                select(ConversationSession.id, ConversationSession.project_id)
                .where(
                    and_(
                        ConversationSession.id == uuid.UUID(session_id),
                        ConversationSession.deleted_at.is_(None),
                        or_(
                            ConversationSession.user_id == user_id,
                            ConversationSession.id.in_(
                                select(ConversationParticipant.session_id).where(
                                    and_(
                                        ConversationParticipant.session_id
                                        == uuid.UUID(session_id),
                                        ConversationParticipant.participant_type
                                        == "user",
                                        ConversationParticipant.participant_id == user_id,
                                        ConversationParticipant.status == "joined",
                                        ConversationParticipant.role.in_(
                                            ["owner", "admin", "member"]
                                        ),
                                    )
                                )
                            ),
                        ),
                    )
                )
                .limit(1)
            )
            row = result.first()
            if row is None:
                return False
            project_id = row[1]
            if project_id is None:
                return True
            try:
                user_uuid = uuid.UUID(str(user_id))
            except (TypeError, ValueError):
                return False
            from .project_repository import ProjectRepository

            return await ProjectRepository.has_permission(
                session, project_id, user_uuid, "write"
            )
        finally:
            if not self._session:
                await session.close()

    # ─── Message CRUD ───────────────────────────────────────────────────

    async def _ensure_linear_parent_links(
        self, session: AsyncSession, session_id: str
    ) -> None:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == uuid.UUID(session_id))
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
        )
        previous: Optional[ConversationMessage] = None
        root_roles: set[str] = set()
        changed = False
        for message in result.scalars().all():
            if message.is_active_branch is None:
                message.is_active_branch = True
                changed = True
            # Root siblings are grouped by role.  A non-zero branch index for
            # an already-seen root role is an explicit branch boundary; keep
            # it instead of attaching it to the preceding active path.  The
            # fallback rows from legacy flat transcripts use branch_index=0
            # (or a role not seen at the root) and still need repair.
            explicit_root_sibling = (
                message.parent_message_id is None
                and message.branch_index not in (None, 0)
                and message.role in root_roles
            )
            if (
                previous
                and message.parent_message_id is None
                and not explicit_root_sibling
            ):
                message.parent_message_id = previous.id
                if message.branch_index is None:
                    message.branch_index = 0
                changed = True
            if message.parent_message_id is None:
                root_roles.add(message.role)
            if message.is_active_branch:
                previous = message
        if changed:
            await session.flush()

    async def _latest_active_message(
        self, session: AsyncSession, session_id: str
    ) -> Optional[ConversationMessage]:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                and_(
                    ConversationMessage.session_id == uuid.UUID(session_id),
                    ConversationMessage.is_active_branch == True,
                )
            )
            .order_by(desc(ConversationMessage.created_at), desc(ConversationMessage.id))
            .limit(1)
        )
        return result.scalar_one_or_none()
    
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        token_count: Optional[int] = None,
        sender_type: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_display_name: Optional[str] = None,
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
            await self._ensure_linear_parent_links(session, session_id)
            result = await session.execute(
                select(ConversationSession.id).where(
                    and_(
                        ConversationSession.id == uuid.UUID(session_id),
                        ConversationSession.deleted_at.is_(None),
                    )
                )
            )
            session_row = result.first()
            if session_row is None:
                raise ValueError("Session not found or deleted")

            parent = await self._latest_active_message(session, session_id)
            parent_message_id = parent.id if parent else None
            branch_index = await self._count_branch_siblings(
                session, session_id, str(parent_message_id) if parent_message_id else None
            )

            message = ConversationMessage(
                session_id=uuid.UUID(session_id),
                role=role,
                content=content,
                parent_message_id=parent_message_id,
                branch_index=branch_index,
                is_active_branch=True,
                message_metadata=metadata or {},
                sender_type=sender_type,
                sender_id=sender_id,
                sender_display_name=sender_display_name,
                token_count=token_count
            )
            session.add(message)
            
            # Persist chat activity only for user/assistant turns. System or
            # maintenance rows must not move the sidebar history.
            update_values = {
                "message_count": ConversationSession.message_count + 1,
            }
            if role in {"user", "assistant"}:
                update_values["last_activity"] = _monotonic_activity(
                    datetime.utcnow()
                )
                update_values["development_status"] = (
                    "working" if role == "user" else "waiting_for_user"
                )
            stmt = update(ConversationSession).where(
                and_(
                    ConversationSession.id == uuid.UUID(session_id),
                    ConversationSession.deleted_at.is_(None),
                )
            ).values(**update_values)
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
        offset: int = 0,
        since: Optional[datetime] = None,
    ) -> List[ConversationMessage]:
        """Get messages for a session

        Args:
            session_id: Session UUID string
            limit: Max messages to return (None for all)
            offset: Offset for pagination
            since: 指定時は updated_at（無ければ created_at）がこの時刻より
                後のメッセージだけを返す差分取得。低帯域環境の再取得量削減用。

        Returns:
            List of ConversationMessage ordered by created_at
        """
        session = await self._get_session()
        try:
            return await self._get_session_messages_in_session(
                session,
                session_id,
                limit=limit,
                offset=offset,
                since=since,
            )
        finally:
            if not self._session:
                await session.close()

    async def _get_session_messages_in_session(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        limit: Optional[int] = None,
        offset: int = 0,
        since: Optional[datetime] = None,
    ) -> List[ConversationMessage]:
        await self._ensure_linear_parent_links(session, session_id)
        query = select(ConversationMessage).where(
            ConversationMessage.session_id == uuid.UUID(session_id)
        ).order_by(ConversationMessage.created_at)

        if since is not None:
            # commit 待ちの行が cursor より古い timestamp を持つ競合でも
            # 取りこぼさないよう 5 秒だけ重ねて取得する。branch切替で
            # inactive化された行もtombstoneとして返し、クライアント側で除去する。
            from datetime import timedelta
            from sqlalchemy import func

            since_floor = since - timedelta(seconds=5)

            query = query.where(
                func.coalesce(
                    ConversationMessage.updated_at,
                    ConversationMessage.created_at,
                )
                > since_floor
            )
        else:
            # 初回全量は現在のactive branchだけを返す。
            query = query.where(
                or_(
                    ConversationMessage.is_active_branch == True,
                    ConversationMessage.is_active_branch.is_(None),
                )
            )

        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)

        result = await session.execute(query)
        return list(result.scalars().all())

    async def get_session_messages_with_branch_info(
        self,
        session_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
        since: Optional[datetime] = None,
    ) -> tuple[List[ConversationMessage], Dict[str, Dict[str, int]]]:
        """Get full/delta messages and their branch projection in one session."""
        session = await self._get_session()
        try:
            messages = await self._get_session_messages_in_session(
                session,
                session_id,
                limit=limit,
                offset=offset,
                since=since,
            )
            branch_info = await self._get_branch_info_for_messages_in_session(
                session, session_id, messages
            )
            return messages, branch_info
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
            result = await session.execute(
                select(ConversationSession.id).where(
                    and_(
                        ConversationSession.id == uuid.UUID(session_id),
                        ConversationSession.deleted_at.is_(None),
                    )
                )
            )
            session_row = result.first()
            if session_row is None:
                raise ValueError("Session not found or deleted")

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
            
            # Persist chat activity only for user/assistant turns. System or
            # maintenance rows must not move the sidebar history.
            update_values = {
                "message_count": ConversationSession.message_count + 1,
            }
            if role in {"user", "assistant"}:
                update_values["last_activity"] = _monotonic_activity(
                    datetime.utcnow()
                )
                update_values["development_status"] = (
                    "working" if role == "user" else "waiting_for_user"
                )
            stmt = update(ConversationSession).where(
                and_(
                    ConversationSession.id == uuid.UUID(session_id),
                    ConversationSession.deleted_at.is_(None),
                )
            ).values(**update_values)
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
            # Repair old flat history before branching so descendants can be
            # deactivated and branch siblings can be found consistently.
            message_result = await session.execute(
                select(ConversationMessage.session_id).where(
                    ConversationMessage.id == uuid.UUID(message_id)
                )
            )
            session_uuid = message_result.scalar_one_or_none()
            if session_uuid:
                await self._ensure_linear_parent_links(session, str(session_uuid))

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
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == original_msg.session_id)
                .values(
                    message_count=ConversationSession.message_count + 1,
                    last_activity=_monotonic_activity(datetime.utcnow()),
                )
            )
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
        message_id: str,
        session_id: Optional[str] = None,
    ):
        """Deactivate a message and every descendant in its session.

        Branch rows are retained, but a branch switch must not leave a stale
        active descendant below an inactive sibling.  Walk every child (not
        only currently-active children) so a previously inconsistent tree is
        repaired as well.  ``session_id`` is optional for existing callers;
        when omitted it is resolved from the root row before any update.
        """
        message_uuid = uuid.UUID(str(message_id))
        if session_id is None:
            session_result = await session.execute(
                select(ConversationMessage.session_id).where(
                    ConversationMessage.id == message_uuid
                )
            )
            session_uuid = session_result.scalar_one_or_none()
            if session_uuid is None:
                return
        else:
            session_uuid = uuid.UUID(str(session_id))

        # Mark the message as inactive, constrained to its conversation.
        stmt = update(ConversationMessage).where(
            and_(
                ConversationMessage.id == message_uuid,
                ConversationMessage.session_id == session_uuid,
            )
        ).values(is_active_branch=False)
        await session.execute(stmt)
        
        # Find and deactivate all child messages recursively.  Include
        # already-inactive children: their descendants may still be stale
        # active rows after a previously interrupted/legacy switch.
        query = select(ConversationMessage).where(
            and_(
                ConversationMessage.session_id == session_uuid,
                ConversationMessage.parent_message_id == message_uuid,
            )
        )
        result = await session.execute(query)
        children = list(result.scalars().all())
        
        for child in children:
            await self._deactivate_branch_from_message(
                session,
                str(child.id),
                str(session_uuid),
            )
    
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
        message_id: str,
        session_id: Optional[str] = None,
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
            message_conditions = [ConversationMessage.id == uuid.UUID(message_id)]
            if session_id is not None:
                message_conditions.append(
                    ConversationMessage.session_id == uuid.UUID(session_id)
                )
            query = select(ConversationMessage).where(and_(*message_conditions))
            result = await session.execute(query)
            message = result.scalar_one_or_none()
            
            if not message:
                return []

            session_id = str(message.session_id)

            await self._ensure_linear_parent_links(session, str(message.session_id))
            await session.refresh(message)
            
            # Find all siblings (same parent_message_id)
            if message.parent_message_id:
                sibling_query = select(ConversationMessage).where(
                    and_(
                        ConversationMessage.session_id == uuid.UUID(session_id),
                        ConversationMessage.parent_message_id == message.parent_message_id,
                    )
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
            
            return [s.to_dict() for s in siblings]
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
            await self._ensure_linear_parent_links(session, session_id)
            session_uuid = uuid.UUID(session_id)
            # Get the target message
            query = select(ConversationMessage).where(
                and_(
                    ConversationMessage.id == uuid.UUID(target_message_id),
                    ConversationMessage.session_id == session_uuid,
                )
            )
            result = await session.execute(query)
            target_msg = result.scalar_one_or_none()
            
            if not target_msg:
                return False
            
            # Load all sibling roots in this session.  Each sibling owns a
            # complete subtree, so deactivating only the sibling rows would
            # leave old descendants active and mix two paths in the result.
            if target_msg.parent_message_id:
                sibling_query = select(ConversationMessage).where(
                    and_(
                        ConversationMessage.session_id == session_uuid,
                        ConversationMessage.parent_message_id == target_msg.parent_message_id,
                    )
                ).order_by(ConversationMessage.branch_index, ConversationMessage.id)
            else:
                # Root level - sibling grouping is by role, as in the branch
                # projection and get_branch_siblings APIs.
                sibling_query = select(ConversationMessage).where(
                    and_(
                        ConversationMessage.session_id == session_uuid,
                        ConversationMessage.parent_message_id.is_(None),
                        ConversationMessage.role == target_msg.role
                    )
                ).order_by(ConversationMessage.branch_index, ConversationMessage.id)
            
            sibling_result = await session.execute(sibling_query)
            siblings = list(sibling_result.scalars().all())
            for sibling in siblings:
                await self._deactivate_branch_from_message(
                    session,
                    str(sibling.id),
                    session_id,
                )
            
            # Activate the target message
            activate_stmt = update(ConversationMessage).where(
                and_(
                    ConversationMessage.id == uuid.UUID(target_message_id),
                    ConversationMessage.session_id == session_uuid,
                )
            ).values(is_active_branch=True)
            await session.execute(activate_stmt)
            
            # Activate descendants in the target branch
            await self._activate_branch_descendants(
                session, target_message_id, session_id
            )
            
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
        message_id: str,
        session_id: str,
    ):
        """Recursively activate the first child branch of a message"""
        # Find children of this message
        query = select(ConversationMessage).where(
            and_(
                ConversationMessage.session_id == uuid.UUID(session_id),
                ConversationMessage.parent_message_id == uuid.UUID(message_id),
            )
        ).order_by(ConversationMessage.branch_index)
        
        result = await session.execute(query)
        children = list(result.scalars().all())
        
        if not children:
            return
        
        # Activate the first child (or the one with lowest branch_index)
        first_child = children[0]
        stmt = update(ConversationMessage).where(
            and_(
                ConversationMessage.id == first_child.id,
                ConversationMessage.session_id == uuid.UUID(session_id),
            )
        ).values(is_active_branch=True)
        await session.execute(stmt)
        
        # Recursively activate its descendants
        await self._activate_branch_descendants(
            session, str(first_child.id), session_id
        )
    
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
            return await self._get_active_branch_messages_in_session(
                session, session_id
            )
        finally:
            if not self._session:
                await session.close()

    async def _get_active_branch_messages_in_session(
        self,
        session: AsyncSession,
        session_id: str,
    ) -> List[ConversationMessage]:
        await self._ensure_linear_parent_links(session, session_id)
        query = select(ConversationMessage).where(
            and_(
                ConversationMessage.session_id == uuid.UUID(session_id),
                ConversationMessage.is_active_branch == True,
            )
        ).order_by(ConversationMessage.created_at)

        result = await session.execute(query)
        return list(result.scalars().all())

    async def get_active_branch_messages_with_branch_info(
        self,
        session_id: str,
    ) -> tuple[List[ConversationMessage], Dict[str, Dict[str, int]]]:
        """Get active messages and their branch projection in one session."""
        session = await self._get_session()
        try:
            messages = await self._get_active_branch_messages_in_session(
                session, session_id
            )
            branch_info = await self._get_branch_info_for_messages_in_session(
                session, session_id, messages
            )
            return messages, branch_info
        finally:
            if not self._session:
                await session.close()

    async def _get_branch_info_for_messages_in_session(
        self,
        session: AsyncSession,
        session_id: str,
        messages: List[ConversationMessage],
    ) -> Dict[str, Dict[str, int]]:
        """Get sibling count and position for messages with one query.

        The caller repairs legacy linear parent links in this same session before
        invoking this helper. Messages with a parent share branches by parent;
        root messages keep the historical role-based grouping used by
        ``get_branch_siblings``.
        """
        if not messages:
            return {}

        parent_ids = {
            message.parent_message_id
            for message in messages
            if message.parent_message_id is not None
        }
        root_roles = {
            message.role
            for message in messages
            if message.parent_message_id is None
        }
        sibling_conditions = []
        if parent_ids:
            sibling_conditions.append(
                ConversationMessage.parent_message_id.in_(parent_ids)
            )
        if root_roles:
            sibling_conditions.append(
                and_(
                    ConversationMessage.parent_message_id.is_(None),
                    ConversationMessage.role.in_(root_roles),
                )
            )
        if not sibling_conditions:
            return {}

        result = await session.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.parent_message_id,
                ConversationMessage.role,
            )
            .where(
                and_(
                    ConversationMessage.session_id == uuid.UUID(session_id),
                    or_(*sibling_conditions),
                )
            )
            .order_by(ConversationMessage.branch_index)
        )

        sibling_groups: Dict[tuple[str, str], List[uuid.UUID]] = {}
        for sibling_id, parent_message_id, role in result.all():
            key = (
                ("parent", str(parent_message_id))
                if parent_message_id is not None
                else ("root", role)
            )
            sibling_groups.setdefault(key, []).append(sibling_id)

        branch_info: Dict[str, Dict[str, int]] = {}
        for message in messages:
            key = (
                ("parent", str(message.parent_message_id))
                if message.parent_message_id is not None
                else ("root", message.role)
            )
            siblings = sibling_groups.get(key, [])
            branch_info[str(message.id)] = {
                "branch_count": len(siblings) or 1,
                "branch_index": next(
                    (
                        index
                        for index, sibling_id in enumerate(siblings)
                        if sibling_id == message.id
                    ),
                    0,
                ),
            }
        return branch_info

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
