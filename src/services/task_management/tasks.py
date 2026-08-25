"""Task management: タスク CRUD / タグ / コメント。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

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
    DocsLibrary,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ...memory.project_repository import ProjectRepository
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
from ..docs_acl import can_write_node
from ..project_color_service import extract_project_color
from ..task_reference_service import attach_agent_run_source_reference
from ..task_project_invariants import (
    lock_task_project_ids,
    prepare_task_parent_update,
    prepare_task_project_move,
)
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
from .helpers import _task_deletion_retention_days

logger = logging.getLogger(__name__)


def _assert_generation_mutation_allowed() -> None:
    """Reject a late generation write immediately before its commit.

    The generation cancellation gate is intentionally resolved lazily here so
    the task service remains importable in lightweight/legacy environments
    that do not load the LLM runtime.  Production generation workers always
    expose the guard module and therefore fail closed when their copied gate
    has been blocked by an interrupt or cancellation.
    """

    try:
        from ...llm.generation_cancellation import (
            raise_if_generation_mutation_blocked,
        )
    except ImportError:
        return
    raise_if_generation_mutation_blocked()


class TaskCrudMixin:
    """タスク CRUD / タグ / コメント。"""

    @staticmethod
    def _coerce_uuid(value: Any) -> Optional[UUID]:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    async def _validate_knowledge_node_binding(
        self,
        session: AsyncSession,
        *,
        knowledge_node_id: UUID,
        task_project_id: UUID,
        user_id: UUID,
    ) -> KnowledgeNode:
        """Validate a Docs node before attaching it to a task.

        A task writer must also be allowed to write the Docs node.  Project
        tasks may bind nodes in their canonical project library, or a
        personal node which the actor explicitly owns/has a write share for;
        a node from another project/library is never accepted.
        """

        node = await session.get(KnowledgeNode, knowledge_node_id)
        if node is None or getattr(node, "archived_at", None) is not None:
            raise TaskManagementError("Docs node not found", status_code=404)
        # ``workspace_id`` is the legacy alias still used by dependency-free
        # service doubles and rolling-deploy callers.  Persisted ORM rows use
        # ``docs_library_id``; accepting the alias here preserves the existing
        # Task/Docs binding API without relaxing the ACL checks below.
        docs_library_id = self._coerce_uuid(
            getattr(node, "docs_library_id", None)
            or getattr(node, "workspace_id", None)
        )
        if docs_library_id is None:
            raise TaskManagementError("Docs node permission denied", status_code=403)
        library = await session.get(DocsLibrary, docs_library_id)
        if (
            library is None
            or self._coerce_uuid(getattr(library, "id", None)) != docs_library_id
        ):
            raise TaskManagementError("Docs node not found", status_code=404)

        try:
            writable = await can_write_node(
                session,
                node,
                user_id,
                library=library,
            )
        except Exception:
            # ACL failures must fail closed.  In particular, a missing share
            # table during a rolling migration must not become a task binding
            # bypass or an unexpected 500.
            writable = False
        if not writable:
            raise TaskManagementError("Docs node permission denied", status_code=403)

        node_project_id = self._coerce_uuid(getattr(node, "project_id", None))
        # Project identity is carried by the canonical node, never by a Docs
        # Library discriminator.  A project-bound node must match the task's
        # project; an ordinary owner-controlled Personal node may remain
        # unbound and still be attached to a task (the task's project is the
        # authoritative task scope in that case).
        if node_project_id is not None and node_project_id != task_project_id:
            raise TaskManagementError("Docs node permission denied", status_code=403)
        return node

    async def _load_task_for_update(
        self,
        session: AsyncSession,
        task_id: UUID,
    ) -> Task:
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
            .with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        return task

    async def create_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        title: str,
        description: Optional[str] = None,
        project_id: Optional[UUID] = None,
        knowledge_node_id: Optional[UUID] = None,
        status: str = "todo",
        priority: Optional[str] = None,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        all_day: bool = False,
        auto_close_on_due: bool = False,
        reminder_offsets: Optional[Iterable[Any]] = None,
        notifications_enabled: Optional[bool] = None,
        estimated_hours: Optional[float] = None,
        parent_task_id: Optional[UUID] = None,
        assignee_ids: Optional[list[UUID]] = None,
        tag_ids: Optional[list[UUID]] = None,
        recurrence_rrule: Optional[str] = None,
        recurrence_timezone: str = DEFAULT_TASK_TIMEZONE,
        task_metadata: Optional[dict[str, Any]] = None,
        source: str = "local",
        legacy_local_task_id: Optional[UUID] = None,
        task_id: Optional[UUID] = None,
        agent_run_id: str | UUID | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        normalized_title = _normalize_task_title(title)
        normalized_status = normalize_task_status(status)
        normalized_priority = normalize_priority(priority)
        normalized_reminders = _ensure_reminder_offsets(reminder_offsets, default=[])
        target_project_id = await self._resolve_project_id(
            session,
            user_id=user_id,
            project_id=project_id,
            require_write=True,
        )
        # A task must be visible to its creator after a successful write.  Do
        # not infer or grant read access here: read/write remain independent
        # membership ACLs, and a write-only member must be rejected before any
        # task rows or related records are touched.
        await self.require_project_permission(
            session,
            project_id=target_project_id,
            user_id=user_id,
            permission="read",
        )
        if knowledge_node_id is not None:
            await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=knowledge_node_id,
                task_project_id=target_project_id,
                user_id=user_id,
            )
        if notifications_enabled is None:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            notifications_enabled = _get_user_task_notifications_default_enabled(user)
        # Web / mobile と同じく、トップレベルは全 Project、サブタスクは
        # 同じ親の中で先頭になるように採番する。
        accessible_project_ids = await self._get_accessible_project_ids(session, user_id)
        sort_project_ids = accessible_project_ids or [target_project_id]
        sort_conditions = [
            Task.project_id.in_(sort_project_ids),
            Task.parent_task_id.is_(None),
            Task.deleted_at.is_(None),
        ]
        if parent_task_id is not None:
            # Parent creation and project moves share the same advisory lock
            # namespace. Lock the project first, then re-read the parent row so
            # a move cannot commit between validation and child insertion.
            await lock_task_project_ids(session, (target_project_id,))
            parent_result = await session.execute(
                select(Task)
                .where(Task.id == parent_task_id, Task.deleted_at.is_(None))
                .with_for_update()
            )
            parent = parent_result.scalar_one_or_none()
            if parent is None:
                raise TaskManagementError("Parent task not found", status_code=404)
            if parent.project_id != target_project_id:
                raise TaskManagementError(
                    "Subtask parent must belong to the same project", status_code=400
                )
            sort_conditions = [
                Task.project_id == target_project_id,
                Task.parent_task_id == parent_task_id,
                Task.deleted_at.is_(None),
            ]
        min_sort_result = await session.execute(
            select(func.min(Task.sort_order)).where(*sort_conditions)
        )
        next_sort_order = float(min_sort_result.scalar_one_or_none() or 0) - 1

        task = Task(
            id=task_id or uuid4(),
            project_id=target_project_id,
            legacy_local_task_id=legacy_local_task_id,
            knowledge_node_id=knowledge_node_id,
            title=normalized_title,
            description=description,
            status=normalized_status,
            priority=normalized_priority,
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            auto_close_on_due=bool(auto_close_on_due),
            reminder_offsets=normalized_reminders,
            notifications_enabled=bool(notifications_enabled),
            estimated_hours=estimated_hours,
            parent_task_id=parent_task_id,
            source=source,
            created_by=user_id,
            completed_at=datetime.utcnow() if normalized_status == "closed" else None,
            task_metadata=_strip_google_calendar_metadata(task_metadata),
            sort_order=next_sort_order,
        )
        session.add(task)
        await session.flush()

        # Agent RunのContextVarに依存せず、呼び出し元で捕捉したRun IDを使う。
        # 参照登録はタスク作成と同じトランザクションに含める。
        await attach_agent_run_source_reference(
            session,
            task_id=task.id,
            project_id=target_project_id,
            user_id=user_id,
            agent_run_id=agent_run_id,
        )

        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=recurrence_rrule,
            timezone=recurrence_timezone,
        )
        await self._replace_assignees(
            session,
            task=task,
            assignee_ids=list(assignee_ids or []),
            assigned_by=user_id,
            assign_requester_when_empty=True,
        )
        if tag_ids:
            await self._replace_tags(session, task=task, tag_ids=tag_ids)
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )
        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
            skip_weekend=bool(recurrence.skip_weekend) if recurrence else False,
            skip_holiday=bool(recurrence.skip_holiday) if recurrence else False,
            skip_mode=recurrence.skip_mode if recurrence else None,
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_created",
            user_id=user_id,
            payload={"project_id": str(target_project_id)},
        )

        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()
        task = await self._load_task(session, task.id)
        payload = task.to_dict()
        if commit:
            await self._broadcast("task_created", payload)
        return payload

    async def list_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        status: Optional[str] = None,
        assignee_id: Optional[UUID] = None,
        search: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        participating_project_ids = await self._get_participating_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            participating_project_ids = (
                [project_id]
                if project_id in participating_project_ids
                else []
            )
        elif space_id is not None:
            participating_project_ids = await self._filter_project_ids_by_space(
                session,
                project_ids=participating_project_ids,
                space_id=space_id,
            )

        if not participating_project_ids:
            return []

        stmt = (
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(
                Task.sort_order.asc().nulls_last(),
                Task.project_id.asc(),
                Task.parent_task_id.asc().nulls_first(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
        )

        if status:
            stmt = stmt.where(Task.status == normalize_task_status(status))
        if search:
            like_term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Task.title.ilike(like_term), Task.description.ilike(like_term))
            )
        if assignee_id:
            stmt = stmt.join(TaskAssignee).where(TaskAssignee.user_id == assignee_id)

        result = await session.execute(stmt)
        return [task.to_dict() for task in result.scalars().unique().all()]

    @staticmethod
    def _task_candidate_snippet(description: str | None, *, max_len: int = 120) -> str:
        text = " ".join((description or "").split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    async def search_task_candidates(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        status: Optional[str] = None,
        assignee_id: Optional[UUID] = None,
        search: Optional[str] = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Return lightweight task candidates for agent duplicate checks."""
        participating_project_ids = await self._get_participating_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            participating_project_ids = (
                [project_id]
                if project_id in participating_project_ids
                else []
            )
        elif space_id is not None:
            participating_project_ids = await self._filter_project_ids_by_space(
                session,
                project_ids=participating_project_ids,
                space_id=space_id,
            )

        if not participating_project_ids:
            return []

        bounded_limit = max(1, min(int(limit or 25), 50))
        stmt = (
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.title,
                    Task.status,
                    Task.project_id,
                    Task.parent_task_id,
                    Task.updated_at,
                    Task.description,
                )
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(
                Task.updated_at.desc(),
                Task.sort_order.asc().nulls_last(),
                Task.id.asc(),
            )
            .limit(bounded_limit)
        )

        if status:
            stmt = stmt.where(Task.status == normalize_task_status(status))
        if search:
            like_term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Task.title.ilike(like_term), Task.description.ilike(like_term))
            )
        if assignee_id:
            stmt = stmt.join(TaskAssignee).where(TaskAssignee.user_id == assignee_id)

        result = await session.execute(stmt)
        candidates: list[dict[str, Any]] = []
        for task in result.scalars().unique().all():
            candidates.append(
                {
                    "id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "project_id": str(task.project_id),
                    "parent_task_id": (
                        str(task.parent_task_id) if task.parent_task_id else None
                    ),
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                    "snippet": self._task_candidate_snippet(task.description),
                }
            )
        return candidates

    async def delete_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> dict[str, Any]:
        """Soft-delete a task tree and return its canonical tombstone.

        The operation is deliberately idempotent: a repeated request returns
        the existing root tombstone instead of creating a second batch.  Only
        rows which are still live participate in a new batch; pre-existing
        tombstones on children/occurrences remain untouched and therefore
        cannot be accidentally restored later.
        """

        # Serialize concurrent deletes of the same root.  Without a row lock,
        # two transactions can each mint a different batch while only one
        # update wins, leaving one caller with a non-existent restore batch.
        result = await session.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if getattr(task, "deleted_at", None) is not None:
            payload = {
                "id": str(task.id),
                "task_id": str(task.id),
                "task_ids": [str(task.id)],
                "deleted_at": task.deleted_at.isoformat(),
                "deletion_batch_id": (
                    str(getattr(task, "deletion_batch_id", None))
                    if getattr(task, "deletion_batch_id", None)
                    else None
                ),
                "idempotent": True,
            }
            await self._broadcast("task_deleted", payload)
            return payload

        task_ids = await self._collect_task_tree_ids(
            session,
            task.id,
            lock_rows=True,
        )
        deleted_at = datetime.utcnow()
        deletion_batch_id = uuid4()

        # Assign the same timestamp and batch to the live task tree.  Updating
        # ORM instances (rather than deleting rows) keeps all comments,
        # activities, references, dependencies and notification history
        # available during the restore window.
        await session.execute(
            update(Task)
            .where(Task.id.in_(task_ids), Task.deleted_at.is_(None))
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id.in_(task_ids),
                TaskOccurrence.deleted_at.is_(None),
            )
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )
        await session.execute(
            update(TimeEntry)
            .where(
                TimeEntry.task_id.in_(task_ids),
                TimeEntry.deleted_at.is_(None),
            )
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )

        # Record audit metadata without making it a prerequisite for the
        # tombstone transaction.  The helper is best-effort by design and is
        # safe while the content-deletion migration rolls out.
        await self._append_task_deletion_audit(
            session,
            task_ids=task_ids,
            deletion_batch_id=deletion_batch_id,
            deleted_at=deleted_at,
            action="delete",
            actor_user_id=user_id,
            root_task_id=task.id,
            project_id=task.project_id,
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_deleted",
            user_id=user_id,
            payload={
                "deletion_batch_id": str(deletion_batch_id),
                "deleted_at": deleted_at.isoformat(),
                "task_ids": [str(value) for value in task_ids],
            },
        )
        _assert_generation_mutation_allowed()
        await session.commit()
        payload = {
            "id": str(task.id),
            "task_id": str(task.id),
            "task_ids": [str(value) for value in task_ids],
            "deleted_at": deleted_at.isoformat(),
            "deletion_batch_id": str(deletion_batch_id),
            "idempotent": False,
        }
        await self._broadcast("task_deleted", payload)
        return payload

    async def restore_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        deletion_batch_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Restore one exact, unexpired task-deletion batch.

        The root's current batch is authoritative.  An optional caller
        supplied id is accepted as an optimistic-concurrency guard; it must
        match exactly.  Rows that were already tombstoned by another batch
        are intentionally excluded from the restore update.
        """

        result = await session.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        root = result.scalar_one_or_none()
        if root is None:
            raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=root.project_id, user_id=user_id, permission="write"
        )

        if root.deleted_at is None:
            if deletion_batch_id is not None:
                raise TaskManagementError(
                    "Deletion batch does not match task", status_code=409
                )
            # Do not call ``to_dict`` here: an idempotent restore may be
            # reached with a minimally-loaded ORM row and async lazy
            # relationships would otherwise raise MissingGreenlet.
            return {
                "id": str(root.id),
                "task_id": str(root.id),
                "restored": False,
                "idempotent": True,
                "deleted_at": None,
                "deletion_batch_id": None,
            }

        batch_id = getattr(root, "deletion_batch_id", None)
        if batch_id is None:
            raise TaskManagementError(
                "Task deletion has no restorable batch", status_code=409
            )
        if deletion_batch_id is not None and deletion_batch_id != batch_id:
            raise TaskManagementError(
                "Deletion batch does not match task", status_code=409
            )

        current_time = now or datetime.utcnow()
        expiry = root.deleted_at + timedelta(days=_task_deletion_retention_days())
        if current_time >= expiry:
            raise TaskManagementError(
                "Task deletion restore window has expired",
                status_code=410,
                detail={
                    "code": "task_restore_expired",
                    "deleted_at": root.deleted_at.isoformat(),
                    "expires_at": expiry.isoformat(),
                },
            )

        deleted_at = root.deleted_at
        task_result = await session.execute(
            select(Task.id).where(
                Task.deletion_batch_id == batch_id,
                Task.deleted_at == deleted_at,
            )
        )
        task_ids = list(task_result.scalars().all())
        if root.id not in task_ids:
            raise TaskManagementError(
                "Deletion batch does not match task", status_code=409
            )

        restored_at = current_time
        await session.execute(
            update(Task)
            .where(
                Task.id.in_(task_ids),
                Task.deletion_batch_id == batch_id,
                Task.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id.in_(task_ids),
                TaskOccurrence.deletion_batch_id == batch_id,
                TaskOccurrence.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await session.execute(
            update(TimeEntry)
            .where(
                TimeEntry.task_id.in_(task_ids),
                TimeEntry.deletion_batch_id == batch_id,
                TimeEntry.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await self._record_activity(
            session,
            task_id=root.id,
            activity_type="task_restored",
            user_id=user_id,
            payload={
                "deletion_batch_id": str(batch_id),
                "restored_at": restored_at.isoformat(),
                "task_ids": [str(value) for value in task_ids],
            },
        )
        await self._append_task_deletion_audit(
            session,
            task_ids=task_ids,
            deletion_batch_id=batch_id,
            deleted_at=deleted_at,
            action="restore",
            actor_user_id=user_id,
            root_task_id=root.id,
            project_id=root.project_id,
            event_at=restored_at,
        )
        _assert_generation_mutation_allowed()
        await session.commit()
        payload = {
            "id": str(root.id),
            "task_id": str(root.id),
            "task_ids": [str(value) for value in task_ids],
            "deletion_batch_id": str(batch_id),
            "restored_at": restored_at.isoformat(),
            "restored": True,
            "idempotent": False,
        }
        await self._broadcast("task_restored", payload)
        return payload

    async def _remove_task_supertags_for_deleted_tasks(
        self,
        session: AsyncSession,
        task_ids: list[UUID],
    ) -> None:
        if not task_ids:
            return
        linked_node_ids = select(Task.knowledge_node_id).where(
            Task.id.in_(task_ids),
            Task.knowledge_node_id.is_not(None),
        )
        task_tag_ids = select(KnowledgeSupertag.id).where(
            KnowledgeSupertag.system_key == "task"
        )
        await session.execute(
            delete(KnowledgeNodeSupertag).where(
                KnowledgeNodeSupertag.node_id.in_(linked_node_ids),
                KnowledgeNodeSupertag.supertag_id.in_(task_tag_ids),
            )
        )

    async def reorder_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        result = await session.execute(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        top_level_ids = set(result.scalars().all())
        requested_ids = set(task_ids)
        if top_level_ids != requested_ids:
            raise TaskManagementError(
                "task_ids must include every top-level task in the project",
                status_code=409,
            )

        for index, task_id in enumerate(task_ids):
            await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.project_id == project_id,
                    Task.parent_task_id.is_(None),
                )
                .values(sort_order=float(index))
            )
        _assert_generation_mutation_allowed()
        await session.commit()

    async def reorder_tasks_global(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        """ALL 表示のトップレベルタスク並び替え（プロジェクト横断）。

        Web BFF の POST /api/tasks/reorder と同じ契約:
        - 重複は除去し、トップレベルかつ未削除のタスクのみ許可
        - 対象タスクが属する全プロジェクトに write 権限が必要
        """
        unique_ids: list[UUID] = []
        seen: set[UUID] = set()
        for task_id in task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            unique_ids.append(task_id)
        if not unique_ids:
            return

        result = await session.execute(
            select(Task).where(
                Task.id.in_(unique_ids),
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        tasks_by_id = {task.id: task for task in result.scalars().all()}
        if len(tasks_by_id) != len(unique_ids):
            raise TaskManagementError(
                "task_ids contains a non top-level or missing task",
                status_code=400,
            )

        for project_id in {task.project_id for task in tasks_by_id.values()}:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )

        for index, task_id in enumerate(unique_ids):
            tasks_by_id[task_id].sort_order = float(index)
        _assert_generation_mutation_allowed()
        await session.commit()

    async def get_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="read"
        )

        active_entry = await self.get_active_time_entry(
            session, user_id=user_id, task_id=task.id
        )
        result = task.to_dict()
        result["comments"] = [comment.to_dict() for comment in task.comments]
        result["activities"] = [activity.to_dict() for activity in task.activities]
        result["occurrences"] = [
            occurrence.to_dict()
            for occurrence in sorted(task.occurrences, key=lambda item: item.start_at)
            if occurrence.deleted_at is None
        ]
        result["time_entries"] = [
            entry.to_dict()
            for entry in task.time_entries
            if entry.deleted_at is None
        ]
        result["active_time_entry"] = active_entry
        return result

    async def update_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        updates: dict[str, Any],
        close_incomplete_subtasks: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        requested_status = updates.get("status")
        is_close_request = (
            requested_status is not None
            and normalize_task_status(str(requested_status)) == "closed"
        )
        # Never take the task row lock before the project advisory lock.  Move
        # callers (REST, sync, and the agent tool) all use
        # ``prepare_task_project_move`` which acquires the sorted project lock
        # namespace first, avoiding a move/dependency/schedule deadlock.
        task = await self._load_task(session, task_id)
        # A few legacy callers provide a lightweight task double only through
        # ``_load_task_for_update`` for close requests. Keep that compatibility
        # path while real ORM tasks always carry ``project_id`` and therefore
        # still follow the advisory-lock-first move path below.
        if is_close_request and not hasattr(task, "project_id"):
            task = await self._load_task_for_update(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        target_project_id = task.project_id
        if "project_id" in updates and updates["project_id"] is not None:
            target_project_id = await self._resolve_project_id(
                session,
                user_id=user_id,
                project_id=updates["project_id"],
                require_write=True,
            )
        project_will_change = target_project_id != task.project_id
        requested_parent_id = (
            updates.get("parent_task_id")
            if "parent_task_id" in updates
            else (None if project_will_change else task.parent_task_id)
        )
        locked_parent: Task | None = None
        if not project_will_change and "parent_task_id" in updates:
            # Same-project reparenting must use the same advisory-lock-first
            # protocol as project moves.  The helper re-reads both rows after
            # locking and rejects a parent whose project changed meanwhile.
            task, locked_parent = await prepare_task_parent_update(
                session,
                task_id=task.id,
                expected_project_id=task.project_id,
                target_parent_task_id=requested_parent_id,
            )
        if is_close_request and not project_will_change:
            # Same-project closes retain the historical confirmation semantics,
            # but acquire the parent row lock before touching its children.
            task = await self._load_task_for_update(session, task_id)

        incomplete_children: list[Task] = []
        if is_close_request and not project_will_change:
            children_result = await session.execute(
                select(Task)
                .where(
                    Task.parent_task_id == task.id,
                    Task.deleted_at.is_(None),
                )
                .with_for_update()
            )
            direct_children = list(children_result.scalars().all())
            incomplete_children = [
                child
                for child in direct_children
                if normalize_task_status(child.status) != "closed"
            ]
            for project_id in {
                child.project_id for child in incomplete_children
            }:
                await self.require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="write",
                )
            if incomplete_children and not close_incomplete_subtasks:
                subtasks = [
                    {
                        "id": str(child.id),
                        "title": child.title,
                        "status": normalize_task_status(child.status),
                    }
                    for child in incomplete_children
                ]
                raise TaskManagementError(
                    "未完了のサブタスクがあります",
                    status_code=409,
                    detail={
                        "code": "incomplete_subtasks_confirmation_required",
                        "detail": "未完了のサブタスクがあります",
                        "incomplete_subtasks": subtasks,
                    },
                )

        parent_was_closed = normalize_task_status(task.status) == "closed"
        completion_time = datetime.utcnow()

        next_knowledge_node_id = (
            updates.get("knowledge_node_id")
            if "knowledge_node_id" in updates
            else task.knowledge_node_id
        )
        if (
            next_knowledge_node_id is not None
            and ("knowledge_node_id" in updates or "project_id" in updates)
        ):
            await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=next_knowledge_node_id,
                task_project_id=target_project_id,
                user_id=user_id,
            )
        if project_will_change:
            task, target_parent = await prepare_task_project_move(
                session,
                task_id=task.id,
                expected_project_id=task.project_id,
                target_project_id=target_project_id,
                target_parent_task_id=requested_parent_id,
            )
            locked_parent = target_parent
            if requested_parent_id is not None:
                task.parent_task_id = target_parent.id if target_parent else None
            # Moving a task with no explicit parent clears its existing parent
            # in the same transaction (the invariant helper has already done
            # so while the task row is locked).
        if "project_id" in updates and updates["project_id"] is not None:
            task.project_id = target_project_id

        if "title" in updates and updates["title"] is not None:
            task.title = _normalize_task_title(str(updates["title"]))
        if "description" in updates:
            task.description = updates["description"]
        if "knowledge_node_id" in updates:
            task.knowledge_node_id = updates["knowledge_node_id"]
        if "status" in updates and updates["status"] is not None:
            task.status = normalize_task_status(updates["status"])
            if task.status == "closed":
                if not parent_was_closed or task.completed_at is None:
                    task.completed_at = completion_time
            else:
                task.completed_at = None
        if "priority" in updates and updates["priority"] is not None:
            task.priority = normalize_priority(updates["priority"])
        if "start_at" in updates:
            task.start_at = updates["start_at"]
        if "end_at" in updates:
            task.end_at = updates["end_at"]
        if "all_day" in updates and updates["all_day"] is not None:
            task.all_day = bool(updates["all_day"])
        if "auto_close_on_due" in updates and updates["auto_close_on_due"] is not None:
            task.auto_close_on_due = bool(updates["auto_close_on_due"])
        if (
            "notifications_enabled" in updates
            and updates["notifications_enabled"] is not None
        ):
            task.notifications_enabled = bool(updates["notifications_enabled"])
        if "reminder_offsets" in updates and updates["reminder_offsets"] is not None:
            task.reminder_offsets = _ensure_reminder_offsets(
                updates["reminder_offsets"],
                default=[],
            )
        if "estimated_hours" in updates:
            task.estimated_hours = updates["estimated_hours"]
        if "parent_task_id" in updates:
            next_parent_id = updates["parent_task_id"]
            if next_parent_id == task.id:
                raise TaskManagementError("Task cannot be its own parent", status_code=400)
            if next_parent_id is not None:
                parent = locked_parent
                if parent is None or parent.id != next_parent_id:
                    # Compatibility fallback for lightweight service doubles;
                    # real writes above always lock the requested parent.
                    parent = await self._load_task(session, next_parent_id)
                if parent.project_id != task.project_id:
                    raise TaskManagementError(
                        "Subtask parent must belong to the same project", status_code=400
                    )
            task.parent_task_id = next_parent_id
        if "task_metadata" in updates and updates["task_metadata"] is not None:
            merged_metadata = dict(task.task_metadata or {})
            merged_metadata.update(updates["task_metadata"])
            task.task_metadata = merged_metadata
        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=updates.get(
                "recurrence_rrule",
                task.recurrence_rule.rrule if task.recurrence_rule else None,
            ),
            timezone=updates.get(
                "recurrence_timezone",
                task.recurrence_rule.timezone
                if task.recurrence_rule
                else DEFAULT_TASK_TIMEZONE,
            ),
        )

        if "assignee_ids" in updates and updates["assignee_ids"] is not None:
            await self._replace_assignees(
                session,
                task=task,
                assignee_ids=list(updates["assignee_ids"]),
                assigned_by=user_id,
            )

        if "tag_ids" in updates and updates["tag_ids"] is not None:
            await self._replace_tags(
                session, task=task, tag_ids=list(updates["tag_ids"])
            )
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )

        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
            skip_weekend=bool(recurrence.skip_weekend) if recurrence else False,
            skip_holiday=bool(recurrence.skip_holiday) if recurrence else False,
            skip_mode=recurrence.skip_mode if recurrence else None,
        )
        for child in incomplete_children:
            previous_status = normalize_task_status(child.status)
            child.status = "closed"
            child.completed_at = completion_time
            child.updated_at = completion_time
            await self._record_activity(
                session,
                task_id=child.id,
                activity_type="closed_by_parent",
                user_id=user_id,
                payload={
                    "parent_task_id": str(task.id),
                    "previous_status": previous_status,
                },
            )

        is_idempotent_close_replay = (
            parent_was_closed
            and not incomplete_children
            and set(updates) == {"status"}
        )
        if not is_idempotent_close_replay:
            activity_payload = {
                key: str(value) for key, value in updates.items() if value is not None
            }
            if incomplete_children:
                activity_payload["closed_incomplete_subtask_count"] = len(
                    incomplete_children
                )
            await self._record_activity(
                session,
                task_id=task.id,
                activity_type="task_updated",
                user_id=user_id,
                payload=activity_payload,
            )
        if "title" in updates and task.knowledge_node_id is not None:
            await self._sync_bound_docs_node_title(
                session,
                task=task,
                user_id=user_id,
            )
        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()
        task = await self._load_task(session, task.id)
        payload = task.to_dict()
        if commit:
            await self._broadcast("task_updated", payload)
        return payload

    async def _sync_bound_docs_node_title(
        self,
        session: AsyncSession,
        *,
        task: Task,
        user_id: UUID,
    ) -> None:
        task_project_id = self._coerce_uuid(getattr(task, "project_id", None))
        if task_project_id is None:
            return
        try:
            node = await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=task.knowledge_node_id,
                task_project_id=task_project_id,
                user_id=user_id,
            )
        except Exception:
            return
        if node.title == task.title:
            return
        node.title = task.title
        node.updated_by = user_id
        node.updated_at = datetime.utcnow()

        # ``tasks.py`` is a submodule of ``services.task_management`` while
        # DocsGraphService lives in ``services``.  The previous single-dot
        # import resolved to the non-existent
        # ``src.services.task_management.docs_graph_service`` package path and
        # made otherwise valid task updates fail when title sync was needed.
        from ..docs_graph_service import DocsGraphService

        await DocsGraphService(session).record_node_change(
            node,
            user_id,
            "タスクタイトルをDocs nodeへ同期",
        )

    async def list_tags(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="read"
        )
        space_id = await self._get_project_space_id(session, project_id=project_id)
        if space_id is None:
            return []
        result = await session.execute(
            select(Tag).where(Tag.space_id == space_id).order_by(Tag.name)
        )
        tags = []
        for tag in result.scalars().all():
            payload = tag.to_dict()
            payload["project_id"] = str(project_id)
            tags.append(payload)
        return tags

    async def create_tag(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        name: str,
        color: Optional[str],
        user_id: UUID,
    ) -> dict[str, Any]:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        name = name.strip()
        if not name:
            raise TaskManagementError("タグ名は必須です", status_code=400)
        space_id = await self._ensure_project_space_id(session, project_id=project_id)
        existing = await session.execute(
            select(Tag).where(Tag.space_id == space_id, Tag.name == name)
        )
        if existing.scalar_one_or_none() is not None:
            raise TaskManagementError("同名のタグが既に存在します", status_code=409)
        tag = Tag(space_id=space_id, name=name, color=color, created_by=user_id)
        session.add(tag)
        _assert_generation_mutation_allowed()
        await session.commit()
        await session.refresh(tag)
        payload = tag.to_dict()
        payload["project_id"] = str(project_id)
        return payload

    async def delete_tag(
        self,
        session: AsyncSession,
        *,
        tag_id: UUID,
        user_id: UUID,
    ) -> None:
        result = await session.execute(select(Tag).where(Tag.id == tag_id))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise TaskManagementError("タグが見つかりません", status_code=404)
        await self._require_space_tag_permission(
            session, space_id=tag.space_id, user_id=user_id, permission="write"
        )
        await session.delete(tag)
        _assert_generation_mutation_allowed()
        await session.commit()

    async def add_comment(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        content: str,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        comment = TaskComment(task_id=task.id, user_id=user_id, content=content.strip())
        session.add(comment)
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="comment_added",
            user_id=user_id,
            payload={"content": content.strip()},
        )
        _assert_generation_mutation_allowed()
        await session.commit()
        await session.refresh(comment)
        await self._broadcast("task_comment_added", comment.to_dict())
        return comment.to_dict()
