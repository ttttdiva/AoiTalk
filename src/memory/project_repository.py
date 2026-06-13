"""
Repository for Project management
"""

import re
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from uuid import UUID, uuid4
from sqlalchemy import select, delete, update, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import (
    Project, ProjectMember, ProjectJoinRequest, User, Space, Tag, TaskTag,
    ConversationSession, LocalTask, Task, TaskAssignee, TaskComment, TaskActivity,
    TaskDependency, TaskRecurrenceRule, TaskOccurrence, TimeEntry,
    ProjectNotificationSetting, NotificationDelivery, KnowledgeSourcePermission,
    RecordField, RecordRow, RecordTable,
)


INBOX_NAME = "Inbox"
INBOX_DESCRIPTION = "未整理のタスクを一時的に置く場所"
INBOX_COLOR = "#6b7280"


def generate_slug(name: str) -> str:
    """Generate URL-safe slug from project name"""
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', name.lower())
    slug = re.sub(r'[-\s]+', '-', slug).strip('-')
    return slug[:100] if slug else 'project'


def user_inbox_project_slug(user_id: UUID) -> str:
    """ユーザー専用 Inbox プロジェクトの slug（フロント ensureInboxDefaultProject と同期）."""
    return f"inbox-project-{user_id}"


def user_inbox_space_slug(user_id: UUID) -> str:
    """ユーザー専用 Inbox スペースの slug（フロント ensureInboxSpace と同期）."""
    return f"inbox-{user_id}"


def legacy_user_default_space_slug(user_id: UUID) -> str:
    """過去の FastAPI 実装が作成していた legacy Default スペース slug."""
    return f"default-{user_id}"


class ProjectRepository:
    """Repository for managing projects and memberships"""

    # ─── User Inbox Project ──────────────────────────────────────────────

    @staticmethod
    async def _get_user_space_by_slug(
        session: AsyncSession, user_id: UUID, slug: str
    ) -> Optional[Space]:
        result = await session.execute(
            select(Space)
            .where(Space.owner_id == user_id, Space.slug == slug)
            .order_by(Space.created_at.asc(), Space.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _merge_legacy_default_space(
        session: AsyncSession,
        *,
        inbox_space: Space,
        legacy_space: Space,
    ) -> None:
        if legacy_space.id == inbox_space.id:
            return

        await session.execute(
            update(Project)
            .where(Project.space_id == legacy_space.id)
            .values(space_id=inbox_space.id)
        )

        legacy_tags = (
            await session.execute(select(Tag).where(Tag.space_id == legacy_space.id))
        ).scalars().all()
        for legacy_tag in legacy_tags:
            existing_result = await session.execute(
                select(Tag).where(
                    Tag.space_id == inbox_space.id,
                    Tag.name == legacy_tag.name,
                )
            )
            existing_tag = existing_result.scalar_one_or_none()
            if existing_tag is None:
                legacy_tag.space_id = inbox_space.id
                continue

            duplicate_task_ids = select(TaskTag.task_id).where(
                TaskTag.tag_id == existing_tag.id
            )
            await session.execute(
                delete(TaskTag).where(
                    TaskTag.tag_id == legacy_tag.id,
                    TaskTag.task_id.in_(duplicate_task_ids),
                )
            )
            await session.execute(
                update(TaskTag)
                .where(TaskTag.tag_id == legacy_tag.id)
                .values(tag_id=existing_tag.id)
            )
            await session.delete(legacy_tag)

        await session.flush()
        await session.delete(legacy_space)

    @staticmethod
    async def ensure_user_inbox_setup(
        session: AsyncSession, user_id: UUID
    ) -> tuple[Space, Project]:
        """ユーザーの既定スペース/プロジェクトを Inbox として保証する。

        過去の FastAPI 経路が作成した `Default` / `default-*` スペースは
        Inbox にリネームまたは吸収し、UI/API へ露出しない。
        """

        inbox_slug = user_inbox_space_slug(user_id)
        legacy_slug = legacy_user_default_space_slug(user_id)

        inbox_space = await ProjectRepository._get_user_space_by_slug(
            session, user_id, inbox_slug
        )
        legacy_space = await ProjectRepository._get_user_space_by_slug(
            session, user_id, legacy_slug
        )

        if inbox_space is None:
            if legacy_space is not None:
                inbox_space = legacy_space
                inbox_space.name = INBOX_NAME
                inbox_space.slug = inbox_slug
                inbox_space.description = inbox_space.description or INBOX_DESCRIPTION
                inbox_space.color = inbox_space.color or INBOX_COLOR
                inbox_space.sort_order = min(inbox_space.sort_order or 0, 0)
            else:
                inbox_space = Space(
                    name=INBOX_NAME,
                    slug=inbox_slug,
                    description=INBOX_DESCRIPTION,
                    color=INBOX_COLOR,
                    owner_id=user_id,
                    sort_order=9999,
                )
                session.add(inbox_space)
            await session.flush()
        elif legacy_space is not None:
            await ProjectRepository._merge_legacy_default_space(
                session,
                inbox_space=inbox_space,
                legacy_space=legacy_space,
            )

        inbox_project_slug = user_inbox_project_slug(user_id)
        project_result = await session.execute(
            select(Project).where(Project.slug == inbox_project_slug)
        )
        inbox_project = project_result.scalar_one_or_none()

        if inbox_project is None:
            inbox_project = Project(
                name=INBOX_NAME,
                slug=inbox_project_slug,
                description=INBOX_DESCRIPTION,
                owner_id=user_id,
                space_id=inbox_space.id,
                project_metadata={
                    "aliases": ["inbox"],
                    "color": INBOX_COLOR,
                    "isInboxDefault": True,
                },
            )
            session.add(inbox_project)
            await session.flush()
        else:
            inbox_project.name = INBOX_NAME
            inbox_project.description = inbox_project.description or INBOX_DESCRIPTION
            inbox_project.space_id = inbox_space.id
            inbox_project.deleted_at = None
            metadata = dict(inbox_project.project_metadata or {})
            aliases = metadata.get("aliases")
            if not isinstance(aliases, list):
                aliases = []
            metadata["aliases"] = sorted({*aliases, "inbox"})
            metadata.setdefault("color", INBOX_COLOR)
            metadata["isInboxDefault"] = True
            inbox_project.project_metadata = metadata

        member_result = await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == inbox_project.id,
                ProjectMember.user_id == user_id,
            )
        )
        if member_result.scalar_one_or_none() is None:
            session.add(
                ProjectMember(
                    project_id=inbox_project.id,
                    user_id=user_id,
                    role="admin",
                )
            )

        await session.flush()
        return inbox_space, inbox_project

    @staticmethod
    async def get_user_inbox_project_id(
        session: AsyncSession, user_id: UUID
    ) -> Optional[UUID]:
        """ユーザー専用 Inbox プロジェクトの ID を返す（無ければ None）。

        Inbox プロジェクトの作成はフロントエンド(/api/spaces GET)が担当する。
        バックエンドは初回ログイン後に存在する前提で参照のみ行う。
        """
        slug = user_inbox_project_slug(user_id)
        result = await session.execute(select(Project.id).where(Project.slug == slug))
        row = result.scalar_one_or_none()
        return row if row else None

    # ─── Project CRUD ───────────────────────────────────────────────────
    
    @staticmethod
    async def create_project(
        session: AsyncSession,
        owner_id: UUID,
        name: str,
        description: Optional[str] = None,
        project_id: Optional[UUID] = None,
        slug: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        allow_join_requests: bool = True,
        storage_quota_mb: int = 1000,
        project_metadata: Optional[Dict[str, Any]] = None,
    ) -> Project:
        """Create a new project
        
        Args:
            session: Database session
            owner_id: UUID of the project owner
            name: Project name
            description: Optional description
            slug: Optional custom slug (auto-generated if not provided)
            allow_join_requests: Whether to accept join requests
            storage_quota_mb: Storage quota in MB
            project_metadata: Optional project metadata payload

        Returns:
            Created Project
        """
        # Generate unique slug
        base_slug = slug or generate_slug(name)
        final_slug = base_slug
        counter = 1
        
        # Check for slug conflicts
        while True:
            existing = await session.execute(
                select(Project).where(Project.slug == final_slug)
            )
            if not existing.scalar_one_or_none():
                break
            final_slug = f"{base_slug}-{counter}"
            counter += 1
        
        project = Project(
            id=project_id or uuid4(),
            name=name,
            description=description,
            slug=final_slug,
            aliases=aliases or [],
            owner_id=owner_id,
            allow_join_requests=allow_join_requests,
            storage_quota_mb=storage_quota_mb,
            project_metadata=project_metadata or {},
        )
        session.add(project)
        await session.flush()
        
        # Add owner as a member with owner role
        owner_member = ProjectMember(
            project_id=project.id,
            user_id=owner_id,
            role='owner',
            permissions={
                'read': True,
                'write': True,
                'delete': True,
                'manage_members': True,
                'manage_settings': True
            }
        )
        session.add(owner_member)
        await session.commit()
        await session.refresh(project)
        
        # Initialize git repository for project's workspace directory
        try:
            from ..services.git_service import ensure_project_git_repository
            ensure_project_git_repository(str(project.id))
        except Exception as e:
            # Log but don't fail project creation
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to initialize git repository for project {project.id}: {e}"
            )
        
        return project
    
    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        project_id: UUID,
        include_members: bool = False
    ) -> Optional[Project]:
        """Get project by ID
        
        Args:
            session: Database session
            project_id: Project UUID
            include_members: Whether to eagerly load members
            
        Returns:
            Project or None
        """
        query = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        if include_members:
            query = query.options(selectinload(Project.members))
        
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_slug(
        session: AsyncSession,
        slug: str,
        include_members: bool = False
    ) -> Optional[Project]:
        """Get project by slug
        
        Args:
            session: Database session
            slug: Project slug
            include_members: Whether to eagerly load members
            
        Returns:
            Project or None
        """
        query = select(Project).where(Project.slug == slug, Project.deleted_at.is_(None))
        if include_members:
            query = query.options(selectinload(Project.members))
        
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_user_projects(
        session: AsyncSession,
        user_id: UUID,
        include_public: bool = False
    ) -> List[Dict[str, Any]]:
        """Get all projects a user has access to
        
        Args:
            session: Database session
            user_id: User UUID
            include_public: Include public projects user is not a member of
            
        Returns:
            List of project dicts with membership info
        """
        # Get projects where user is a member
        query = (
            select(Project, ProjectMember)
            .join(ProjectMember, Project.id == ProjectMember.project_id)
            .where(ProjectMember.user_id == user_id, Project.deleted_at.is_(None))
            .order_by(Project.updated_at.desc())
        )
        
        result = await session.execute(query)
        projects = []
        
        for project, member in result.fetchall():
            proj_dict = project.to_dict()
            proj_dict['membership'] = member.to_dict()
            projects.append(proj_dict)
        
        return projects
    
    @staticmethod
    async def update_project(
        session: AsyncSession,
        project_id: UUID,
        **kwargs
    ) -> Optional[Project]:
        """Update project fields
        
        Args:
            session: Database session
            project_id: Project UUID
            **kwargs: Fields to update (name, description,
                      allow_join_requests, storage_quota_mb, project_metadata)
            
        Returns:
            Updated Project or None
        """
        allowed_fields = {
            'name', 'description', 'aliases', 'allow_join_requests',
            'storage_quota_mb', 'project_metadata'
        }
        
        update_data = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not update_data:
            return await ProjectRepository.get_by_id(session, project_id)
        
        update_data['updated_at'] = datetime.utcnow()
        
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(**update_data)
        )
        await session.commit()
        
        return await ProjectRepository.get_by_id(session, project_id)
    
    @staticmethod
    async def delete_project(
        session: AsyncSession,
        project_id: UUID
    ) -> bool:
        """Soft-delete a project and tombstone syncable descendants.

        Sync 対象の `projects` / `tasks` / `task_occurrences` / `time_entries`
        は `deleted_at` tombstone を付与する。非同期対象の補助テーブルは
        既存どおり物理削除し、会話セッションは project_id を NULL にして保持する。
        """
        project = await ProjectRepository.get_by_id(session, project_id)
        if project is None:
            return False

        now = datetime.utcnow()

        # タスク配下を tombstone 化
        await session.execute(
            update(TimeEntry)
            .where(
                TimeEntry.task_id.in_(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.deleted_at.is_(None),
                    )
                ),
                TimeEntry.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now)
        )
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id.in_(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.deleted_at.is_(None),
                    )
                ),
                TaskOccurrence.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now)
        )
        await session.execute(
            update(Task)
            .where(Task.project_id == project_id, Task.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )

        # 非sync補助テーブルは物理削除
        task_ids_result = await session.execute(
            select(Task.id).where(Task.project_id == project_id)
        )
        task_ids = [row[0] for row in task_ids_result.all()]
        if task_ids:
            await session.execute(delete(TaskRecurrenceRule).where(TaskRecurrenceRule.task_id.in_(task_ids)))
            await session.execute(delete(TaskDependency).where(TaskDependency.task_id.in_(task_ids)))
            await session.execute(delete(TaskActivity).where(TaskActivity.task_id.in_(task_ids)))
            await session.execute(delete(TaskComment).where(TaskComment.task_id.in_(task_ids)))
            await session.execute(delete(TaskAssignee).where(TaskAssignee.task_id.in_(task_ids)))

        await session.execute(delete(NotificationDelivery).where(NotificationDelivery.project_id == project_id))
        await session.execute(delete(LocalTask).where(LocalTask.project_id == project_id))
        await session.execute(delete(ProjectNotificationSetting).where(ProjectNotificationSetting.project_id == project_id))
        await session.execute(delete(KnowledgeSourcePermission).where(KnowledgeSourcePermission.project_id == project_id))
        await session.execute(delete(ProjectJoinRequest).where(ProjectJoinRequest.project_id == project_id))
        await session.execute(
            update(RecordRow)
            .where(RecordRow.project_id == project_id, RecordRow.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
        await session.execute(
            update(RecordField)
            .where(
                RecordField.table_id.in_(
                    select(RecordTable.id).where(RecordTable.project_id == project_id)
                ),
                RecordField.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now)
        )
        await session.execute(
            update(RecordTable)
            .where(RecordTable.project_id == project_id, RecordTable.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )

        # 会話セッションは project_id を外して残す
        await session.execute(
            update(ConversationSession)
            .where(ConversationSession.project_id == project_id)
            .values(project_id=None, updated_at=now)
        )

        project.deleted_at = now
        project.updated_at = now
        await session.commit()
        return True
    
    # ─── Member Management ──────────────────────────────────────────────
    
    @staticmethod
    async def add_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        role: str = 'member',
        invited_by: Optional[UUID] = None,
        permissions: Optional[Dict[str, bool]] = None
    ) -> Optional[ProjectMember]:
        """Add a member to project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID to add
            role: Member role ('admin', 'member', 'viewer')
            invited_by: UUID of user who invited
            permissions: Custom permissions override
            
        Returns:
            ProjectMember or None if already exists
        """
        # Check if already a member
        existing = await session.execute(
            select(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        if existing.scalar_one_or_none():
            return None
        
        # Default permissions by role
        default_permissions = {
            'owner': {'read': True, 'write': True, 'delete': True, 'manage_members': True, 'manage_settings': True},
            'admin': {'read': True, 'write': True, 'delete': True, 'manage_members': True, 'manage_settings': False},
            'member': {'read': True, 'write': True, 'delete': False, 'manage_members': False, 'manage_settings': False},
            'viewer': {'read': True, 'write': False, 'delete': False, 'manage_members': False, 'manage_settings': False}
        }
        
        member = ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            permissions=permissions or default_permissions.get(role, default_permissions['member'])
        )
        session.add(member)
        await session.commit()
        await session.refresh(member)
        
        return member
    
    @staticmethod
    async def get_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID
    ) -> Optional[ProjectMember]:
        """Get membership info for a user in a project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            
        Returns:
            ProjectMember or None
        """
        result = await session.execute(
            select(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_project_members(
        session: AsyncSession,
        project_id: UUID
    ) -> List[Dict[str, Any]]:
        """Get all members of a project with user info
        
        Args:
            session: Database session
            project_id: Project UUID
            
        Returns:
            List of member dicts with user info
        """
        result = await session.execute(
            select(ProjectMember, User)
            .join(User, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.joined_at)
        )
        
        members = []
        for member, user in result.fetchall():
            member_dict = member.to_dict()
            member_dict['user'] = {
                'id': str(user.id),
                'username': user.username,
                'display_name': user.display_name,
                'email': user.email
            }
            members.append(member_dict)
        
        return members
    
    @staticmethod
    async def update_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        role: Optional[str] = None,
        permissions: Optional[Dict[str, bool]] = None
    ) -> Optional[ProjectMember]:
        """Update member role or permissions
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            role: New role
            permissions: New permissions
            
        Returns:
            Updated ProjectMember or None
        """
        update_data = {}
        if role is not None:
            update_data['role'] = role
        if permissions is not None:
            update_data['permissions'] = permissions
        
        if not update_data:
            return await ProjectRepository.get_member(session, project_id, user_id)
        
        await session.execute(
            update(ProjectMember)
            .where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
            .values(**update_data)
        )
        await session.commit()
        
        return await ProjectRepository.get_member(session, project_id, user_id)
    
    @staticmethod
    async def remove_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID
    ) -> bool:
        """Remove a member from project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID to remove
            
        Returns:
            bool: True if removed
        """
        # Cannot remove owner
        member = await ProjectRepository.get_member(session, project_id, user_id)
        if member and member.role == 'owner':
            return False
        
        result = await session.execute(
            delete(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        await session.commit()
        return result.rowcount > 0
    
    # ─── Join Request Management ────────────────────────────────────────
    
    @staticmethod
    async def create_join_request(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        message: Optional[str] = None
    ) -> Optional[ProjectJoinRequest]:
        """Create a join request
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID requesting to join
            message: Optional request message
            
        Returns:
            ProjectJoinRequest or None if already member/pending
        """
        # Check if already a member
        existing_member = await ProjectRepository.get_member(session, project_id, user_id)
        if existing_member:
            return None
        
        # Check for existing pending request
        existing_request = await session.execute(
            select(ProjectJoinRequest).where(
                and_(
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.user_id == user_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
        )
        if existing_request.scalar_one_or_none():
            return None
        
        request = ProjectJoinRequest(
            project_id=project_id,
            user_id=user_id,
            message=message
        )
        session.add(request)
        await session.commit()
        await session.refresh(request)
        
        return request
    
    @staticmethod
    async def get_pending_requests(
        session: AsyncSession,
        project_id: UUID
    ) -> List[Dict[str, Any]]:
        """Get pending join requests for a project
        
        Args:
            session: Database session
            project_id: Project UUID
            
        Returns:
            List of request dicts with user info
        """
        result = await session.execute(
            select(ProjectJoinRequest, User)
            .join(User, ProjectJoinRequest.user_id == User.id)
            .where(
                and_(
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
            .order_by(ProjectJoinRequest.created_at)
        )
        
        requests = []
        for req, user in result.fetchall():
            req_dict = req.to_dict()
            req_dict['user'] = {
                'id': str(user.id),
                'username': user.username,
                'display_name': user.display_name
            }
            requests.append(req_dict)
        
        return requests
    
    @staticmethod
    async def approve_join_request(
        session: AsyncSession,
        request_id: UUID,
        approved_by: UUID,
        role: str = 'member'
    ) -> Optional[ProjectMember]:
        """Approve a join request
        
        Args:
            session: Database session
            request_id: Request UUID
            approved_by: UUID of approving user
            role: Role to assign to new member
            
        Returns:
            ProjectMember if approved, None otherwise
        """
        # Get request
        result = await session.execute(
            select(ProjectJoinRequest).where(ProjectJoinRequest.id == request_id)
        )
        request = result.scalar_one_or_none()
        
        if not request or request.status != 'pending':
            return None
        
        # Update request status
        await session.execute(
            update(ProjectJoinRequest)
            .where(ProjectJoinRequest.id == request_id)
            .values(
                status='approved',
                processed_by=approved_by,
                processed_at=datetime.utcnow()
            )
        )
        
        # Add as member
        member = await ProjectRepository.add_member(
            session,
            request.project_id,
            request.user_id,
            role=role,
            invited_by=approved_by
        )
        
        return member
    
    @staticmethod
    async def reject_join_request(
        session: AsyncSession,
        request_id: UUID,
        rejected_by: UUID,
        reason: Optional[str] = None
    ) -> bool:
        """Reject a join request
        
        Args:
            session: Database session
            request_id: Request UUID
            rejected_by: UUID of rejecting user
            reason: Optional rejection reason
            
        Returns:
            bool: True if rejected
        """
        result = await session.execute(
            update(ProjectJoinRequest)
            .where(
                and_(
                    ProjectJoinRequest.id == request_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
            .values(
                status='rejected',
                processed_by=rejected_by,
                processed_at=datetime.utcnow(),
                rejection_reason=reason
            )
        )
        await session.commit()
        return result.rowcount > 0
    
    # ─── Utility Methods ────────────────────────────────────────────────
    
    @staticmethod
    async def has_permission(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        permission: str
    ) -> bool:
        """Check if user has specific permission in project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            permission: Permission key to check
            
        Returns:
            bool: True if has permission
        """
        member = await ProjectRepository.get_member(session, project_id, user_id)
        if not member:
            return False
        
        permissions = member.permissions or {}
        return permissions.get(permission, False)
    
    @staticmethod
    async def get_storage_path(project_id: UUID) -> str:
        """Get the storage path for a project
        
        Args:
            project_id: Project UUID
            
        Returns:
            str: Relative path to project storage
        """
        return f"_projects/project_{project_id}"
    
    @staticmethod
    async def get_user_storage_path(user_id: UUID) -> str:
        """Get the storage path for a user's personal storage
        
        Args:
            user_id: User UUID
            
        Returns:
            str: Relative path to user storage
        """
        return f"_users/user_{user_id}"
