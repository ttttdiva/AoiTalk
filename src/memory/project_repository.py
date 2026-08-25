"""
Repository for Project management
"""

import asyncio
import re
import logging
import os
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
    TaskReference,
    ProjectNotificationSetting, NotificationDelivery, KnowledgeSourcePermission,
    KnowledgeSource, ProjectContextPack, ContextMemory, RecordAttachment,
    RecordEvent, RecordField, RecordRow, RecordTable, RecordView,
    AppGrant, AppJob, ProjectApp,
)
from ..services.project_permissions import (
    PROJECT_MEMBER_DEFAULT_PERMISSIONS,
    get_default_project_permissions,
    has_effective_project_permission,
    normalize_project_member_permissions,
    normalize_project_member_role,
)


INBOX_NAME = "Inbox"
INBOX_DESCRIPTION = "未整理のタスクを一時的に置く場所"
INBOX_COLOR = "#6b7280"
OWNER_MEMBER_PERMISSIONS = PROJECT_MEMBER_DEFAULT_PERMISSIONS["owner"]

logger = logging.getLogger(__name__)


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

        if inbox_project is not None and inbox_project.owner_id != user_id:
            # The slug embeds the UUID and is reserved for exactly one user.
            # Never repair/reassign a colliding project: doing so would leave
            # the previous owner with a still-valid membership and turn a
            # harmless slug collision into an ownership takeover.
            raise RuntimeError("Reserved Inbox project slug is owned by another user")

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
            inbox_project.owner_id = user_id
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
        owner_member = member_result.scalar_one_or_none()
        if owner_member is None:
            session.add(
                ProjectMember(
                    project_id=inbox_project.id,
                    user_id=user_id,
                    role="owner",
                    permissions=dict(OWNER_MEMBER_PERMISSIONS),
                )
            )
        else:
            # Inbox の所有者は project.owner_id と常に一致させる。過去の
            # FastAPI/Next 実装が作った admin・権限なし membership も、
            # ログイン時の冪等なセットアップで安全な所有者権限へ修復する。
            owner_member.role = "owner"
            owner_member.permissions = dict(OWNER_MEMBER_PERMISSIONS)

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
        result = await session.execute(
            select(Project.id).where(
                Project.slug == slug,
                Project.owner_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
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
        space_id: Optional[UUID] = None,
        is_completed: bool = False,
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
            space_id: Optional owning Space UUID
            is_completed: Whether the project is in the completed section
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
            space_id=space_id,
            is_completed=bool(is_completed),
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
    async def get_by_id_for_update(
        session: AsyncSession,
        project_id: UUID,
    ) -> Optional[Project]:
        """Load an active project while holding its row lock.

        File uploads use this lock to serialize the quota check, filesystem
        write, and tracked usage update across Python and Next.js workers.
        """
        result = await session.execute(
            select(Project)
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
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
        from ..services.project_permissions import normalize_project_member_permissions

        user = await session.get(User, user_id)
        if getattr(user, "role", None) == "admin":
            result = await session.execute(
                select(Project)
                .where(Project.deleted_at.is_(None))
                .order_by(Project.updated_at.desc())
            )
            return [project.to_dict() for project in result.scalars().all()]

        # Get projects where user is a member, then apply the same explicit
        # read policy used by project context and task services.
        query = (
            select(Project, ProjectMember)
            .outerjoin(
                ProjectMember,
                and_(
                    Project.id == ProjectMember.project_id,
                    ProjectMember.user_id == user_id,
                ),
            )
            .where(
                Project.deleted_at.is_(None),
                or_(Project.owner_id == user_id, ProjectMember.user_id == user_id),
            )
            .order_by(Project.updated_at.desc())
        )
        
        result = await session.execute(query)
        projects = []
        
        for project, member in result.fetchall():
            permissions = normalize_project_member_permissions(
                member.permissions if member else None
            )
            if project.owner_id != user_id and permissions.get("read") is not True:
                continue
            proj_dict = project.to_dict()
            proj_dict['membership'] = member.to_dict() if member else None
            projects.append(proj_dict)
        
        return projects

    @staticmethod
    async def get_accessible_project_ids(
        session: AsyncSession,
        user_id: UUID,
    ) -> List[UUID]:
        """Return project IDs the user may access for management/read paths.

        ``accessible`` intentionally includes global administrators, project
        owners, and members with an explicit effective ``read`` grant.  A
        membership row by itself (or its role name) is not a grant; malformed
        ACL JSON is denied by :func:`normalize_project_member_permissions`.
        This helper is read-only and never repairs or creates memberships.
        """
        user = await session.get(User, user_id)
        if getattr(user, "role", None) == "admin":
            result = await session.execute(
                select(Project.id).where(Project.deleted_at.is_(None))
            )
            return list(result.scalars().all())

        owned_result = await session.execute(
            select(Project.id).where(
                Project.owner_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        accessible = list(owned_result.scalars().all())
        accessible_set = set(accessible)

        result = await session.execute(
            select(ProjectMember, Project.owner_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                ProjectMember.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        for member, owner_id in result.all():
            permissions = normalize_project_member_permissions(member.permissions)
            if owner_id == user_id or permissions.get("read") is True:
                if member.project_id not in accessible_set:
                    accessible.append(member.project_id)
                    accessible_set.add(member.project_id)
        return accessible

    @staticmethod
    async def get_participating_project_ids(
        session: AsyncSession,
        user_id: UUID,
    ) -> List[UUID]:
        """Return operationally participating project IDs for ``user_id``.

        Participation is deliberately narrower than management/read access:
        project owners participate, as do users with an explicit effective
        ``ProjectMember.permissions.read`` grant.  A global-admin role alone
        does *not* make every project participating.  The query is strictly
        read-only so scope calculation cannot create implicit memberships.
        """
        owned_result = await session.execute(
            select(Project.id).where(
                Project.owner_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        participating = list(owned_result.scalars().all())
        participating_set = set(participating)

        result = await session.execute(
            select(ProjectMember, Project.owner_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                ProjectMember.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        for member, owner_id in result.all():
            permissions = normalize_project_member_permissions(member.permissions)
            if owner_id == user_id or permissions.get("read") is True:
                if member.project_id not in participating_set:
                    participating.append(member.project_id)
                    participating_set.add(member.project_id)
        return participating
    
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
                      space_id, is_completed, allow_join_requests,
                      storage_quota_mb, project_metadata)
            
        Returns:
            Updated Project or None
        """
        allowed_fields = {
            'name', 'description', 'aliases', 'allow_join_requests',
            'space_id', 'is_completed', 'storage_quota_mb', 'project_metadata'
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
        project_id: UUID,
        *,
        delete_workspace: bool = False,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> bool:
        from ..services.app_operation_lock import project_operation_lock
        from ..services.app_storage import get_workspaces_root

        # ロック path を決める root と、実際に消す workspace の root がずれると
        # 排他が静かに壊れる。呼び出し元が None を渡した場合も含めて、実効 root を
        # ここで 1 度だけ確定し、ロックと削除処理へ同じ値を渡す。
        effective_root = get_workspaces_root(workspace_root)
        async with project_operation_lock(project_id, workspace_root=effective_root):
            return await ProjectRepository._delete_project_unlocked(
                session,
                project_id,
                delete_workspace=delete_workspace,
                workspace_root=effective_root,
            )

    @staticmethod
    async def _delete_project_unlocked(
        session: AsyncSession,
        project_id: UUID,
        *,
        delete_workspace: bool = False,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> bool:
        """Delete a project from the active app surface.

        Sync 対象の `projects` / `tasks` / `task_occurrences` / `time_entries`
        は `deleted_at` tombstone を付与する。台帳補助データ、
        メンバー、ナレッジ、コンテキストなどの非sync補助データは物理削除し、
        会話セッションは project_id を NULL にして保持する。
        """
        project = await ProjectRepository.get_by_id(session, project_id)
        if project is None:
            return False

        # Serialize deletion with ProjectApp link/update/unlink operations.
        # The binding routes acquire this same Project row lock before
        # mutating the relationship.
        locked_project_id = await session.scalar(select(Project.id).where(
            Project.id == project_id,
            Project.deleted_at.is_(None),
        ).with_for_update())
        if locked_project_id is None:
            return False

        now = datetime.utcnow()

        # Project App instances are disposable, but an active App job may
        # still have that instance as its cwd/log destination.  Cancel active
        # jobs before deleting the binding; row locks make a queued executor
        # observe the cancelled state after this transaction commits.
        project_app_result = await session.execute(
            select(ProjectApp.app_id).where(ProjectApp.project_id == project_id)
        )
        project_app_ids = list(project_app_result.scalars().all())
        if project_app_ids:
            await session.execute(
                select(ProjectApp.app_id)
                .where(
                    ProjectApp.project_id == project_id,
                    ProjectApp.app_id.in_(project_app_ids),
                )
                .with_for_update()
            )

        # AppJob の検索条件は project_id だけで、ProjectApp binding には依存しない。
        # run 実行中に unlink（ProjectApp 削除）してから Project を削除すると
        # binding は 0 件になるが、下の ``remove_app_instance`` は
        # ``_app_instances/project_<id>`` を丸ごと消す。binding の有無でここを
        # ガードすると、実行中サブプロセスの cwd とログ出力先が生きたまま消える
        # （Windows は使用中で rmtree 失敗 → 部分削除のまま握り潰し、Linux は
        # unlink 済みファイルへ書き続ける）。よって走査は常に行う。
        active_jobs_result = await session.execute(
            select(AppJob)
            .where(
                AppJob.project_id == project_id,
                AppJob.status.in_(("queued", "running")),
            )
            .with_for_update()
        )
        active_jobs = list(active_jobs_result.scalars().all())
        if active_jobs:
            from ..services.app_job_service import stop_running_job

            for job in active_jobs:
                if job.status == "running":
                    # stop_running_job → _kill_process_tree は Windows で
                    # subprocess.run(["taskkill", ...], timeout=10) を同期実行する。
                    # event loop 上で直接呼ぶと Job 1 件あたり最大 10 秒、
                    # サーバー全体が無応答になるため必ず別スレッドへ逃がす。
                    await asyncio.to_thread(stop_running_job, job.id)
                job.status = "cancelled"
                job.result_json = {"error": "Project was deleted"}
                job.ended_at = now

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
            await session.execute(delete(TaskReference).where(TaskReference.task_id.in_(task_ids)))

        record_table_ids_result = await session.execute(
            select(RecordTable.id).where(RecordTable.project_id == project_id)
        )
        record_table_ids = [row[0] for row in record_table_ids_result.all()]
        record_row_ids_result = await session.execute(
            select(RecordRow.id).where(RecordRow.project_id == project_id)
        )
        record_row_ids = [row[0] for row in record_row_ids_result.all()]
        if record_row_ids:
            await session.execute(delete(RecordAttachment).where(RecordAttachment.row_id.in_(record_row_ids)))
        if record_table_ids:
            await session.execute(delete(RecordView).where(RecordView.table_id.in_(record_table_ids)))
        await session.execute(delete(RecordEvent).where(RecordEvent.project_id == project_id))

        await session.execute(delete(ProjectContextPack).where(ProjectContextPack.project_id == project_id))
        await session.execute(delete(ContextMemory).where(ContextMemory.project_id == project_id))
        await session.execute(delete(NotificationDelivery).where(NotificationDelivery.project_id == project_id))
        await session.execute(delete(LocalTask).where(LocalTask.project_id == project_id))
        await session.execute(delete(ProjectNotificationSetting).where(ProjectNotificationSetting.project_id == project_id))
        await session.execute(delete(KnowledgeSourcePermission).where(KnowledgeSourcePermission.project_id == project_id))
        await session.execute(delete(ProjectJoinRequest).where(ProjectJoinRequest.project_id == project_id))
        await session.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id))

        project_workspace_sources = await session.execute(
            select(KnowledgeSource).where(KnowledgeSource.source_type == "project_workspace")
        )
        for source in project_workspace_sources.scalars().all():
            access_policy = source.access_policy or {}
            if str(access_policy.get("project_id") or "") == str(project_id):
                await session.delete(source)

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
            .values(project_id=None)
        )

        project.deleted_at = now
        project.updated_at = now
        # Project deletion is a soft delete, so database FK cascades do not run.
        # App source/artifacts remain first-class resources, while bindings and
        # Project-scoped grants must be removed explicitly.
        await session.execute(delete(ProjectApp).where(ProjectApp.project_id == project_id))
        await session.execute(delete(AppGrant).where(AppGrant.project_id == project_id))
        await session.commit()
        from ..services.app_storage import remove_app_instance

        # The DB tombstone/binding deletion is the transaction boundary.  Do
        # not destroy filesystem data before it commits: a DB rollback must
        # leave the instance available for the still-existing binding.
        if delete_workspace:
            try:
                from ..services.project_workspace_cleanup import remove_project_workspace

                # ロック取得と同じ root を使わないと、別 root の workspace を
                # 削除してしまう（= ロックが守っていない領域を触る）。
                remove_project_workspace(project_id, workspace_root=workspace_root)
            except Exception:
                logger.exception("Project workspace cleanup failed after Project deletion: %s", project_id)
        try:
            remove_app_instance(project_id, workspace_root=workspace_root)
        except Exception:
            logger.exception("App instance cleanup failed after Project deletion: %s", project_id)
        return True

    @staticmethod
    async def delete_projects_in_space(
        session: AsyncSession,
        space_id: UUID,
        *,
        delete_workspaces: bool = False,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> int:
        """Soft-delete active projects in a space, then detach all project refs.

        Space deletion must not leave active projects with ``space_id = NULL``.
        Deleted project rows are detached afterward so the space row can be
        removed without violating the ``projects.space_id`` foreign key.
        """
        from ..services.app_storage import get_workspaces_root

        result = await session.execute(
            select(Project.id).where(
                Project.space_id == space_id,
                Project.deleted_at.is_(None),
            )
        )
        project_ids = list(result.scalars().all())

        # Space 配下のすべての Project 削除を同じ実効 root で処理する。
        effective_root = get_workspaces_root(workspace_root)
        deleted_count = 0
        for project_id in project_ids:
            if await ProjectRepository.delete_project(
                session,
                project_id,
                delete_workspace=delete_workspaces,
                workspace_root=effective_root,
            ):
                deleted_count += 1

        await session.execute(
            update(Project)
            .where(Project.space_id == space_id)
            .values(space_id=None, updated_at=datetime.utcnow())
        )
        return deleted_count
    
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
        # Membership mutations share the project row lock with storage
        # writers.  This makes an upload that acquired the lock first finish
        # under the old ACL, while an upload that waits observes this change.
        project = await ProjectRepository.get_by_id_for_update(session, project_id)
        if project is None:
            return None

        normalized_role = normalize_project_member_role(role)
        if normalized_role == "owner" and project.owner_id != user_id:
            raise ValueError("Only the project owner may have owner role")
        if permissions is not None and not isinstance(permissions, dict):
            raise ValueError("Project member permissions must be an object")

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
        
        member = ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=normalized_role,
            invited_by=invited_by,
            permissions=(
                dict(permissions)
                if permissions is not None
                else get_default_project_permissions(normalized_role)
            )
        )
        session.add(member)
        # ACL changes are part of the project sync revision.  Bumping the
        # project timestamp lets delta clients refresh the canonical project
        # set after a grant/revoke without exposing membership rows directly.
        project.updated_at = datetime.utcnow()
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
            select(ProjectMember)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id,
                    Project.deleted_at.is_(None),
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
    async def get_project_assignee_candidates(
        session: AsyncSession,
        project_id: UUID,
    ) -> List[Dict[str, Any]]:
        """Return active project members with only task-assignment fields."""
        result = await session.execute(
            select(
                ProjectMember.user_id,
                User.username,
                User.display_name,
            )
            .join(User, ProjectMember.user_id == User.id)
            .where(
                ProjectMember.project_id == project_id,
                User.is_active.is_(True),
            )
            .order_by(ProjectMember.joined_at)
        )
        return [
            {
                "user_id": str(user_id),
                "username": username,
                "display_name": display_name,
            }
            for user_id, username, display_name in result.all()
        ]
    
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
        project = await ProjectRepository.get_by_id_for_update(session, project_id)
        if project is None or project.owner_id == user_id:
            return None

        normalized_role = None
        if role is not None:
            normalized_role = normalize_project_member_role(role)
            if normalized_role == "owner":
                return None
        if permissions is not None and not isinstance(permissions, dict):
            raise ValueError("Project member permissions must be an object")

        update_data = {}
        if normalized_role is not None:
            update_data['role'] = normalized_role
            if permissions is None:
                update_data['permissions'] = get_default_project_permissions(
                    normalized_role
                )
        if permissions is not None:
            update_data['permissions'] = dict(permissions)
        
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
        project.updated_at = datetime.utcnow()
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
        project = await ProjectRepository.get_by_id_for_update(session, project_id)
        if project is None or project.owner_id == user_id:
            return False

        # Keep the persisted role from becoming a second owner, even for
        # legacy projects whose membership role was manually edited.
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
        if result.rowcount > 0:
            project.updated_at = datetime.utcnow()
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
        
        # The legacy schema has a full (project_id, user_id) unique constraint,
        # not a partial pending-only index. Reuse a rejected row so a user can
        # apply again without turning a normal request into a 500.
        existing_request = await session.execute(
            select(ProjectJoinRequest).where(
                and_(
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.user_id == user_id,
                )
            )
        )
        prior_request = existing_request.scalar_one_or_none()
        if not prior_request:
            request = ProjectJoinRequest(
                project_id=project_id,
                user_id=user_id,
                message=message,
            )
            session.add(request)
        elif prior_request.status == "pending":
            return None
        elif prior_request.status == "rejected":
            prior_request.message = message
            prior_request.status = "pending"
            prior_request.processed_by = None
            prior_request.processed_at = None
            prior_request.rejection_reason = None
            prior_request.created_at = datetime.utcnow()
            request = prior_request
        else:
            # An approved request should have a membership. Do not mutate it
            # implicitly if the database is inconsistent.
            return None
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
        project_id: UUID,
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
            select(ProjectJoinRequest).where(
                and_(
                    ProjectJoinRequest.id == request_id,
                    ProjectJoinRequest.project_id == project_id,
                )
            )
        )
        request = result.scalar_one_or_none()
        
        if not request or request.status != 'pending':
            return None
        
        # Update request status
        await session.execute(
            update(ProjectJoinRequest)
            .where(
                and_(
                    ProjectJoinRequest.id == request_id,
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.status == "pending",
                )
            )
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
        project_id: UUID,
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
                    ProjectJoinRequest.project_id == project_id,
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
        # Use one fresh query instead of the ORM identity map's potentially
        # stale Project/User/ProjectMember instances.  Callers that already
        # hold the project row lock therefore make the ACL decision against
        # the committed membership visible after that lock was acquired.
        result = await session.execute(
            select(Project.owner_id, User.role, ProjectMember.permissions)
            .select_from(Project)
            .join(User, User.id == user_id)
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == user_id,
                ),
            )
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
        row = result.one_or_none()
        if row is None:
            return False

        owner_id, user_role, member_permissions = row
        return has_effective_project_permission(
            user_id=user_id,
            user_role=user_role,
            project_owner_id=owner_id,
            member_permissions=member_permissions,
            permission=permission,
        )
    
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
