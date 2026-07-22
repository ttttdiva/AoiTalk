"""Task management: 共有ヘルパー: 権限・プロジェクト解決・タスクツリー等。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...memory.models import (
    NotificationDelivery,
    Project,
    ProjectNotificationSetting,
    ProjectMember,
    Space,
    Tag,
    Task,
    TaskActivity,
    TaskAssignee,
    TaskAttachment,
    TaskReference,
    TaskComment,
    TaskDependency,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    User,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ...memory.project_repository import ProjectRepository
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
from ..project_color_service import extract_project_color
from ..task_reference_service import attach_agent_run_source_reference
from ._shared import (
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_USER_NOTIFICATION_MINUTES,
    DISALLOWED_PLACEHOLDER_TITLES,
    LEGACY_STATUS_MAP,
    VALID_PRIORITIES,
    VALID_TASK_STATUSES,
    ScheduledOccurrence,
    TaskManagementError,
    build_occurrence_schedule,
    build_time_report,
    correct_likely_timer_started_at,
    normalize_priority,
    normalize_task_status,
    _ensure_reminder_offsets,
    _get_user_notification_minutes,
    _get_user_task_notifications_default_enabled,
    _is_date_only_occurrence,
    _is_midnight,
    _normalize_member_permissions,
    _normalize_task_title,
    _strip_google_calendar_metadata,
)

logger = logging.getLogger(__name__)


class HelperMixin:
    """共有ヘルパー: 権限・プロジェクト解決・タスクツリー等。"""

    async def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._broadcaster:
            return
        try:
            await self._broadcaster({"type": event_type, "data": data})
        except Exception:
            logger.exception("Task event broadcast failed: %s", event_type)

    async def _get_accessible_project_ids(
        self, session: AsyncSession, user_id: UUID
    ) -> list[UUID]:
        result = await session.execute(
            select(ProjectMember.project_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.user_id == user_id, Project.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    async def _filter_project_ids_by_space(
        self,
        session: AsyncSession,
        *,
        project_ids: list[UUID],
        space_id: UUID,
    ) -> list[UUID]:
        if not project_ids:
            return []
        result = await session.execute(
            select(Project.id).where(
                Project.id.in_(project_ids),
                Project.space_id == space_id,
                Project.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())

    async def _ensure_inbox_membership(
        self, session: AsyncSession, user_id: UUID
    ) -> UUID:
        await ProjectRepository.ensure_user_inbox_setup(session, user_id)
        inbox_id = await ProjectRepository.get_user_inbox_project_id(session, user_id)
        if inbox_id is None:
            raise TaskManagementError(
                "Inbox project is not available for this user", status_code=503
            )
        membership = await ProjectRepository.get_member(session, inbox_id, user_id)
        if membership is None:
            await ProjectRepository.add_member(
                session,
                project_id=inbox_id,
                user_id=user_id,
                role="member",
            )
        return inbox_id

    async def _resolve_project_id(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID],
        require_write: bool = True,
    ) -> UUID:
        if project_id is None:
            project_id = await self._ensure_inbox_membership(session, user_id)

        await self.require_project_permission(
            session,
            project_id=project_id,
            user_id=user_id,
            permission="write" if require_write else "read",
        )
        return project_id

    async def require_project_permission(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
        permission: str,
    ) -> None:
        member = await ProjectRepository.get_member(session, project_id, user_id)
        project = await ProjectRepository.get_by_id(session, project_id)
        if project is None:
            raise TaskManagementError("Project not found", status_code=404)
        if member is None:
            raise TaskManagementError("Project access denied", status_code=403)

        if member.role in {"owner", "admin"}:
            return

        permissions = _normalize_member_permissions(member.role, member.permissions)
        if permission == "read" and permissions.get("read", False):
            return
        if permission == "write" and permissions.get("write", False):
            return
        if permission == "delete" and permissions.get("delete", False):
            return
        if permission == "manage_settings" and (
            permissions.get("manage_settings", False)
            or permissions.get("manage_notifications", False)
        ):
            return
        raise TaskManagementError("Project permission denied", status_code=403)

    async def _load_task(self, session: AsyncSession, task_id: UUID) -> Task:
        result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.comments).selectinload(TaskComment.user),
                selectinload(Task.activities).selectinload(TaskActivity.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.occurrences),
                selectinload(Task.time_entries).selectinload(TimeEntry.user),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(Task.id == task_id, Task.deleted_at.is_(None))
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        return task

    async def _collect_task_tree_ids(
        self, session: AsyncSession, root_task_id: UUID
    ) -> list[UUID]:
        task_ids = [root_task_id]
        seen = {root_task_id}
        queue = [root_task_id]

        while queue:
            result = await session.execute(
                select(Task.id).where(Task.parent_task_id.in_(queue))
            )
            child_ids = [
                child_id
                for child_id in result.scalars().all()
                if child_id not in seen
            ]
            if not child_ids:
                break
            seen.update(child_ids)
            task_ids.extend(child_ids)
            queue = child_ids

        return task_ids

    async def _replace_assignees(
        self,
        session: AsyncSession,
        *,
        task: Task,
        assignee_ids: list[UUID],
        assigned_by: UUID,
    ) -> None:
        await session.execute(
            delete(TaskAssignee).where(TaskAssignee.task_id == task.id)
        )
        unique_ids = []
        seen: set[UUID] = set()
        for assignee_id in assignee_ids:
            if assignee_id in seen:
                continue
            seen.add(assignee_id)
            unique_ids.append(assignee_id)

        if not unique_ids:
            unique_ids = [assigned_by]

        for index, assignee_id in enumerate(unique_ids):
            session.add(
                TaskAssignee(
                    task_id=task.id,
                    user_id=assignee_id,
                    is_primary=index == 0,
                    assigned_by=assigned_by,
                )
            )

    async def _replace_tags(
        self,
        session: AsyncSession,
        *,
        task: Task,
        tag_ids: list[UUID],
    ) -> None:
        await session.execute(delete(TaskTag).where(TaskTag.task_id == task.id))
        seen: set[UUID] = set()
        for tag_id in tag_ids:
            if tag_id in seen:
                continue
            seen.add(tag_id)
            session.add(TaskTag(task_id=task.id, tag_id=tag_id))

    async def _get_or_create_repeat_tag(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
    ) -> UUID:
        """スペース内の 'repeat' タグを取得し、なければ作成してIDを返す。"""
        space_id = await self._ensure_project_space_id(session, project_id=project_id)
        result = await session.execute(
            select(Tag).where(Tag.space_id == space_id, Tag.name == "repeat")
        )
        tag = result.scalar_one_or_none()
        if tag is None:
            tag = Tag(space_id=space_id, name="repeat", color="#6366f1")
            session.add(tag)
            await session.flush()
        return tag.id

    async def _get_project_space_id(
        self, session: AsyncSession, *, project_id: UUID
    ) -> Optional[UUID]:
        result = await session.execute(
            select(Project.space_id).where(Project.id == project_id)
        )
        return result.scalar_one_or_none()

    async def _ensure_project_space_id(
        self, session: AsyncSession, *, project_id: UUID
    ) -> UUID:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project is None:
            raise TaskManagementError("Project not found", status_code=404)
        if project.space_id is not None:
            return project.space_id

        slug = f"default-{project.owner_id}"
        existing = await session.execute(
            select(Space).where(Space.owner_id == project.owner_id, Space.slug == slug)
        )
        space = existing.scalar_one_or_none()
        if space is None:
            space = Space(
                name="Default",
                slug=slug,
                owner_id=project.owner_id,
                sort_order=0,
            )
            session.add(space)
            await session.flush()
        project.space_id = space.id
        await session.flush()
        return space.id

    async def _require_space_tag_permission(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        user_id: UUID,
        permission: str,
    ) -> None:
        result = await session.execute(
            select(Project.id)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(
                Project.space_id == space_id,
                ProjectMember.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        for project_id in result.scalars().all():
            try:
                await self.require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission=permission,
                )
                return
            except TaskManagementError:
                continue
        raise TaskManagementError("Project permission denied", status_code=403)

    async def _record_activity(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        activity_type: str,
        user_id: Optional[UUID],
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        session.add(
            TaskActivity(
                task_id=task_id,
                user_id=user_id,
                activity_type=activity_type,
                payload=payload or {},
            )
        )

