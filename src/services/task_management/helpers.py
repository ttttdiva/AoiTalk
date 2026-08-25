"""Task management: 共有ヘルパー: 権限・プロジェクト解決・タスクツリー等。"""

from __future__ import annotations

import inspect
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
    TaskRelation,
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
    _normalize_task_title,
    _strip_google_calendar_metadata,
)

logger = logging.getLogger(__name__)


# Normal task deletion is reversible for the shared content-deletion retention
# period.  Keep a thirty-day fallback for rolling deployments where the
# optional policy helper has not landed yet.
try:
    from ..content_deletion_service import get_deletion_retention_days
except ImportError:  # pragma: no cover - only mixed-version deployments
    def get_deletion_retention_days(value: Any = None) -> int:
        return 30


def _task_deletion_retention_days(value: Any = None) -> int:
    return int(get_deletion_retention_days(value))


def _assert_generation_mutation_allowed() -> None:
    """Reject a late generation write immediately before committing cleanup."""

    try:
        from ...llm.generation_cancellation import (
            raise_if_generation_mutation_blocked,
        )
    except ImportError:
        return
    raise_if_generation_mutation_blocked()


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
        return await ProjectRepository.get_accessible_project_ids(session, user_id)

    async def _get_participating_project_ids(
        self, session: AsyncSession, user_id: UUID
    ) -> list[UUID]:
        """Resolve operational project scope without global-admin expansion.

        Management/direct URL/Docs callers continue to use
        :meth:`_get_accessible_project_ids`.  Aggregates (tasks, calendar,
        time entries and reports) should call this narrower helper so a global
        admin does not silently receive every project's operational data.
        """
        return await ProjectRepository.get_participating_project_ids(session, user_id)

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
        project = await ProjectRepository.get_by_id(session, project_id)
        if project is None:
            raise TaskManagementError("Project not found", status_code=404)

        if await ProjectRepository.has_permission(
            session,
            project_id=project_id,
            user_id=user_id,
            permission=permission,
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
        self,
        session: AsyncSession,
        root_task_id: UUID,
        *,
        lock_rows: bool = False,
    ) -> list[UUID]:
        task_ids = [root_task_id]
        seen = {root_task_id}
        queue = [root_task_id]

        while queue:
            statement = select(Task.id).where(
                    Task.parent_task_id.in_(queue),
                    Task.deleted_at.is_(None),
                )
            if lock_rows:
                # Lock each discovered level before the bulk tombstone update
                # so a concurrent child DELETE cannot create a second batch
                # between tree discovery and root deletion.
                statement = statement.with_for_update()
            result = await session.execute(statement)
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

    async def _append_task_deletion_audit(
        self,
        session: AsyncSession,
        *,
        task_ids: Iterable[UUID],
        deletion_batch_id: UUID,
        deleted_at: datetime,
        action: str,
        actor_user_id: Optional[UUID] = None,
        root_task_id: Optional[UUID] = None,
        event_at: Optional[datetime] = None,
        project_id: Optional[UUID] = None,
    ) -> None:
        """Best-effort bridge to the shared content-deletion audit service.

        The audit service is introduced independently of the task rollout. A
        lazy import keeps this service import-safe while workers are on mixed
        revisions; once present, support its keyword-oriented API without
        coupling task code to a concrete implementation signature.
        """

        from ..content_deletion_service import append_content_deletion_event

        normalized_action = {
            "delete": "deleted",
            "restore": "restored",
            "purge": "purged",
        }.get(action, action)
        ids = list(task_ids)
        if not ids:
            return
        root_id = str(root_task_id or ids[0])
        for task_id in ids:
            result = append_content_deletion_event(
                session,
                "task",
                str(task_id),
                action=normalized_action,
                root_entity_id=root_id,
                batch_id=deletion_batch_id,
                project_id=project_id,
                actor_user_id=actor_user_id,
                source="task_management",
                event_at=event_at or deleted_at,
                metadata={
                    "retention_days": _task_deletion_retention_days(),
                    "deleted_at": deleted_at.isoformat(),
                },
            )
            if inspect.isawaitable(result):
                await result

    async def purge_expired_task_deletions(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        retention_days: Optional[int] = None,
        limit: Optional[int] = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Physically remove task batches whose restore window has expired.

        Normal DELETE never removes rows.  This helper is intentionally
        separate so a scheduled retention worker (or an explicitly approved
        maintenance command) can perform the irreversible step after the
        thirty-day window.  Rows are deleted in dependency order and the
        parent self-reference is detached before task rows are removed.
        """

        retention_days = (
            _task_deletion_retention_days()
            if retention_days is None
            else int(retention_days)
        )
        if retention_days < 0:
            raise TaskManagementError("retention_days must be non-negative", status_code=400)
        cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
        batch_stmt = (
            select(Task.deletion_batch_id, func.min(Task.deleted_at))
            .where(
                Task.deleted_at.is_not(None),
                Task.deleted_at < cutoff,
                Task.deletion_batch_id.is_not(None),
            )
            .group_by(Task.deletion_batch_id)
            .order_by(func.min(Task.deleted_at).asc())
        )
        if limit is not None:
            batch_stmt = batch_stmt.limit(max(0, int(limit)))
        batch_result = await session.execute(batch_stmt)
        batch_rows = list(batch_result.all())
        if not batch_rows:
            return {
                "purged_batches": 0,
                "purged_tasks": 0,
                "cutoff": cutoff.isoformat(),
            }

        purged_batches = 0
        purged_tasks = 0
        for batch_id, batch_deleted_at in batch_rows:
            if batch_id is None:
                continue
            task_result = await session.execute(
                select(Task.id, Task.project_id).where(
                    Task.deletion_batch_id == batch_id
                )
            )
            task_rows = list(task_result.all())
            task_ids = [row[0] for row in task_rows]
            batch_project_id = task_rows[0][1] if task_rows else None
            if not task_ids:
                continue

            await self._append_task_deletion_audit(
                session,
                task_ids=task_ids,
                deletion_batch_id=batch_id,
                deleted_at=batch_deleted_at or cutoff,
                action="purge",
                event_at=now or datetime.utcnow(),
                project_id=batch_project_id,
            )
            await self._remove_task_supertags_for_deleted_tasks(session, task_ids)

            # Keep the explicit order even though most installations also
            # declare ON DELETE CASCADE.  This works against older rolling
            # schemas and preserves task/time-entry audit rows until purge.
            await session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.task_id.in_(task_ids)
                )
            )
            await session.execute(delete(TimeEntry).where(TimeEntry.task_id.in_(task_ids)))
            await session.execute(
                delete(TaskOccurrence).where(TaskOccurrence.task_id.in_(task_ids))
            )
            await session.execute(
                delete(TaskDependency).where(
                    or_(
                        TaskDependency.task_id.in_(task_ids),
                        TaskDependency.depends_on_task_id.in_(task_ids),
                    )
                )
            )
            await session.execute(
                delete(TaskActivity).where(TaskActivity.task_id.in_(task_ids))
            )
            await session.execute(
                delete(TaskRecurrenceRule).where(TaskRecurrenceRule.task_id.in_(task_ids))
            )
            await session.execute(delete(TaskComment).where(TaskComment.task_id.in_(task_ids)))
            await session.execute(
                delete(TaskAttachment).where(TaskAttachment.task_id.in_(task_ids))
            )
            await session.execute(
                delete(TaskReference).where(TaskReference.task_id.in_(task_ids))
            )
            await session.execute(
                delete(TaskRelation).where(
                    or_(
                        TaskRelation.task_a_id.in_(task_ids),
                        TaskRelation.task_b_id.in_(task_ids),
                    )
                )
            )
            await session.execute(delete(TaskTag).where(TaskTag.task_id.in_(task_ids)))
            await session.execute(
                delete(TaskAssignee).where(TaskAssignee.task_id.in_(task_ids))
            )
            # Detach the self-referential parent links before deleting the
            # batch; otherwise a non-deferrable FK can reject parent removal.
            await session.execute(
                update(Task)
                .where(Task.id.in_(task_ids))
                .values(parent_task_id=None)
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            purged_batches += 1
            purged_tasks += len(task_ids)

        if commit and (purged_batches or purged_tasks):
            _assert_generation_mutation_allowed()
            await session.commit()
        return {
            "purged_batches": purged_batches,
            "purged_tasks": purged_tasks,
            "cutoff": cutoff.isoformat(),
        }

    # Keep a discoverable spelling for retention workers written against the
    # initial task-lifecycle design.
    async def purge_deleted_task_batches(self, session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        return await self.purge_expired_task_deletions(session, **kwargs)

    async def _replace_assignees(
        self,
        session: AsyncSession,
        *,
        task: Task,
        assignee_ids: list[UUID],
        assigned_by: UUID,
        assign_requester_when_empty: bool = False,
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

        if not unique_ids and assign_requester_when_empty:
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

