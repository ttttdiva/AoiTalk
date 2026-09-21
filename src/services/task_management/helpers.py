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
from ..project_permissions import normalize_project_member_permissions
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

    @staticmethod
    def _normalize_browse_uuid(value: Any, label: str) -> UUID | None:
        """Normalize an explicit browse target without treating bad input as absent."""

        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return UUID(value.strip())
            except (TypeError, ValueError, AttributeError):
                pass
        raise TaskManagementError(f"Invalid {label}", status_code=400)

    @staticmethod
    def _is_reserved_inbox_project(project: Project) -> bool:
        metadata = getattr(project, "project_metadata", None)
        return bool(
            getattr(project, "slug", None)
            == f"inbox-project-{getattr(project, 'owner_id', None)}"
            or (
                isinstance(metadata, dict)
                and metadata.get("isInboxDefault") is True
            )
        )

    @staticmethod
    def _is_reserved_inbox_space(space: Space) -> bool:
        return getattr(space, "slug", None) == f"inbox-{getattr(space, 'owner_id', None)}"

    async def resolve_browse_project_ids(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        browse_project_id: UUID | str | None = None,
        browse_space_id: UUID | str | None = None,
    ) -> list[UUID]:
        """Resolve one explicit, read-only browse target to exact project IDs.

        The resolver intentionally uses the accessible/read ACL (including the
        existing global-admin elevation) rather than participation.  It never
        repairs memberships or broadens a target beyond the requested Project or
        Space.  Ineligible targets are normalized to 404 to avoid an ACL oracle.
        """

        project_target = self._normalize_browse_uuid(
            browse_project_id, "browse_project_id"
        )
        space_target = self._normalize_browse_uuid(
            browse_space_id, "browse_space_id"
        )
        if project_target is not None and space_target is not None:
            raise TaskManagementError(
                "browse_project_id and browse_space_id are mutually exclusive",
                status_code=400,
            )
        if project_target is None and space_target is None:
            raise TaskManagementError(
                "An explicit browse target is required", status_code=400
            )

        if project_target is not None:
            project = await ProjectRepository.get_by_id(session, project_target)
            if project is None or (
                self._is_reserved_inbox_project(project)
                and str(getattr(project, "owner_id", "")) != str(user_id)
            ):
                raise TaskManagementError("Browse target not found", status_code=404)
            if not await ProjectRepository.has_permission(
                session,
                project_id=project_target,
                user_id=user_id,
                permission="read",
            ):
                raise TaskManagementError("Browse target not found", status_code=404)
            return [project_target]

        space_result = await session.execute(
            select(Space).where(Space.id == space_target)
        )
        space = space_result.scalar_one_or_none()
        if space is None or (
            self._is_reserved_inbox_space(space)
            and str(getattr(space, "owner_id", "")) != str(user_id)
        ):
            raise TaskManagementError("Browse target not found", status_code=404)

        accessible_ids = await self._get_accessible_project_ids(session, user_id)
        project_result = await session.execute(
            select(Project)
            .where(
                Project.id.in_(accessible_ids or [UUID(int=0)]),
                Project.space_id == space_target,
                Project.deleted_at.is_(None),
            )
            .order_by(Project.id.asc())
        )
        projects = [
            project
            for project in project_result.scalars().all()
            if not (
                self._is_reserved_inbox_project(project)
                and str(getattr(project, "owner_id", "")) != str(user_id)
            )
        ]
        if (
            not projects
            and str(getattr(space, "owner_id", "")) != str(user_id)
        ):
            user_result = await session.execute(
                select(User.role).where(User.id == user_id)
            )
            user_role = user_result.scalar_one_or_none()
            if str(user_role or "").lower() != "admin":
                raise TaskManagementError("Browse target not found", status_code=404)
        return [project.id for project in projects]

    async def resolve_read_project_ids(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID | None = None,
        space_id: UUID | None = None,
        browse_project_id: UUID | str | None = None,
        browse_space_id: UUID | str | None = None,
    ) -> list[UUID]:
        """Resolve the legacy participating scope or an explicit browse scope."""

        has_browse = browse_project_id is not None or browse_space_id is not None
        if has_browse:
            if project_id is not None or space_id is not None:
                raise TaskManagementError(
                    "browse scope cannot be combined with project_id or space_id",
                    status_code=400,
                )
            return await self.resolve_browse_project_ids(
                session,
                user_id=user_id,
                browse_project_id=browse_project_id,
                browse_space_id=browse_space_id,
            )

        participating_project_ids = await self._get_participating_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            return [project_id] if project_id in participating_project_ids else []
        if space_id is not None:
            return await self._filter_project_ids_by_space(
                session,
                project_ids=participating_project_ids,
                space_id=space_id,
            )
        return participating_project_ids

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
                selectinload(Task.project).selectinload(Project.space),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.comments).selectinload(TaskComment.user),
                selectinload(Task.activities).selectinload(TaskActivity.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.occurrences),
                selectinload(Task.time_entries).selectinload(TimeEntry.user),
                selectinload(Task.time_entries).selectinload(TimeEntry.occurrence),
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
        # Validate the complete replacement set before deleting any existing
        # rows.  An assignee is a durable notification recipient, so accepting
        # an arbitrary UUID (or a user whose Project membership was revoked)
        # would turn task assignment into a cross-project data leak.
        unique_ids = []
        seen: set[UUID] = set()
        for assignee_id in assignee_ids:
            if assignee_id in seen:
                continue
            seen.add(assignee_id)
            unique_ids.append(assignee_id)

        if not unique_ids and assign_requester_when_empty:
            unique_ids = [assigned_by]

        # ``task.project_id`` is non-null on persisted Task rows.  Tiny
        # dependency-free test doubles historically omitted it; keep those
        # calls side-effect-only while production rows always take the strict
        # validation path below.
        task_project_id = getattr(task, "project_id", None)
        if task_project_id is not None and unique_ids:
            project = await session.get(Project, task_project_id)
            if project is None or getattr(project, "deleted_at", None) is not None:
                raise TaskManagementError("Project not found", status_code=404)
            for assignee_id in unique_ids:
                try:
                    assignee_uuid = UUID(str(assignee_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise TaskManagementError(
                        "Assignee is not an active Project member", status_code=403
                    ) from exc
                user = await session.get(User, assignee_uuid)
                if user is None or not bool(getattr(user, "is_active", False)):
                    # Use one uniform denial to avoid turning assignment into
                    # a user-existence oracle for outsiders/inactive accounts.
                    raise TaskManagementError(
                        "Assignee is not an active Project member", status_code=403
                    )
                owner_id = getattr(project, "owner_id", None)
                if owner_id is not None and str(owner_id) == str(assignee_uuid):
                    continue
                member = await ProjectRepository.get_member(
                    session,
                    task_project_id,
                    assignee_uuid,
                )
                permissions = normalize_project_member_permissions(
                    getattr(member, "permissions", None) if member is not None else None
                )
                if member is None or permissions.get("read") is not True:
                    raise TaskManagementError(
                        "Assignee is not an active Project member", status_code=403
                    )

        await session.execute(
            delete(TaskAssignee).where(TaskAssignee.task_id == task.id)
        )

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
        created_at: datetime | None = None,
    ) -> TaskActivity:
        activity = TaskActivity(
            # Assign the identity before flush so the completion candidate can
            # use the same UUID as its episode fence in this transaction.
            id=uuid4(),
            task_id=task_id,
            user_id=user_id,
            activity_type=activity_type,
            payload=payload or {},
            created_at=created_at or datetime.utcnow(),
        )
        session.add(activity)
        return activity

