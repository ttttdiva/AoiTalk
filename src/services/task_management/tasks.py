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


class TaskCrudMixin:
    """タスク CRUD / タグ / コメント。"""

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
            parent = await self._load_task(session, parent_task_id)
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
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_created",
            user_id=user_id,
            payload={"project_id": str(target_project_id)},
        )

        if commit:
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
        accessible_project_ids = await self._get_accessible_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            accessible_project_ids = [project_id]
        elif space_id is not None:
            accessible_project_ids = await self._filter_project_ids_by_space(
                session, project_ids=accessible_project_ids, space_id=space_id
            )

        if not accessible_project_ids:
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
                Task.project_id.in_(accessible_project_ids),
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

    async def delete_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> None:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        task_ids = await self._collect_task_tree_ids(session, task.id)
        await self._remove_task_supertags_for_deleted_tasks(session, task_ids)

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
        await session.execute(
            delete(TaskComment).where(TaskComment.task_id.in_(task_ids))
        )
        await session.execute(
            delete(TaskAttachment).where(TaskAttachment.task_id.in_(task_ids))
        )
        await session.execute(
            delete(TaskReference).where(TaskReference.task_id.in_(task_ids))
        )
        await session.execute(delete(TaskTag).where(TaskTag.task_id.in_(task_ids)))
        await session.execute(
            delete(TaskAssignee).where(TaskAssignee.task_id.in_(task_ids))
        )
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        await session.commit()
        await self._broadcast(
            "task_deleted",
            {"task_id": str(task_id), "task_ids": [str(value) for value in task_ids]},
        )

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
        ]
        result["time_entries"] = [entry.to_dict() for entry in task.time_entries]
        result["active_time_entry"] = active_entry
        return result

    async def update_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if "project_id" in updates and updates["project_id"] is not None:
            target_project_id = await self._resolve_project_id(
                session,
                user_id=user_id,
                project_id=updates["project_id"],
                require_write=True,
            )
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
                task.completed_at = datetime.utcnow()
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
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_updated",
            user_id=user_id,
            payload={
                key: str(value) for key, value in updates.items() if value is not None
            },
        )
        if "title" in updates and task.knowledge_node_id is not None:
            await self._sync_bound_docs_node_title(
                session,
                task=task,
                user_id=user_id,
            )
        await session.commit()
        task = await self._load_task(session, task.id)
        await self._broadcast("task_updated", task.to_dict())
        return task.to_dict()

    async def _sync_bound_docs_node_title(
        self,
        session: AsyncSession,
        *,
        task: Task,
        user_id: UUID,
    ) -> None:
        node = await session.get(KnowledgeNode, task.knowledge_node_id)
        if node is None or node.archived_at is not None or node.title == task.title:
            return
        node.title = task.title
        node.updated_by = user_id
        node.updated_at = datetime.utcnow()

        from .docs_graph_service import DocsGraphService

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
        await session.commit()
        await session.refresh(comment)
        await self._broadcast("task_comment_added", comment.to_dict())
        return comment.to_dict()
