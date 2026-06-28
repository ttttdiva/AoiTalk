"""タスクのCRUD・割り当て・スケジュール関連ツール。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from ...tools.core import tool

from ...task_time import DEFAULT_TASK_TIMEZONE
from .common import (
    _run_async,
    _normalize_task_schedule_inputs,
    _resolve_actor_and_project,
    _parse_ids,
    _json,
)


def build_task_tools() -> list:
    """タスクのCRUD・割り当て・スケジュール関連ツールのツール群を生成して返す。"""

    @tool
    def list_tasks(
        project: str = "", project_id: str = "", status: str = "", search: str = ""
    ) -> str:
        """List tasks for a project. `project` accepts a UUID, slug, or project name."""
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                service = TaskManagementService()
                tasks = await service.list_tasks(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                    status=status or None,
                    search=search or None,
                )
                return tasks
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @tool
    def create_task(
        title: str,
        description: str = "",
        project: str = "",
        project_id: str = "",
        due_date: str = "",
        start_at: str = "",
        end_at: str = "",
        all_day: bool = False,
        priority: str = "medium",
        assignee_ids: str = "",
        recurrence_rrule: str = "",
        recurrence_timezone: str = DEFAULT_TASK_TIMEZONE,
    ) -> str:
        """Create a task.

        Args:
            title: Concise user-facing task title. Infer it from the concrete action/event. For reservation emails, use only venue/service + purpose such as "予約先 来店（サービス名）"; do not include appointment dates/times, parenthesized dates/times, generic labels like "予約確認タスク", or reservation numbers as the main title.
            description: Supporting details such as reservation number, date/time, price, coupon/point usage, contact information, cancellation notes, and the source email facts.
            project: UUID, slug, or project name. Omit it to use the current project or Inbox.
            project_id: Explicit project UUID. Omit it to use the current project or Inbox.
            due_date: Date-only planned/due day in YYYY-MM-DD format when the task has a 予定日, deadline, or appointment day but no specific time. Use this instead of only mentioning the date in the response.
            start_at: Task start datetime when the content contains an appointment or scheduled work time.
            end_at: Task end datetime when known.
            all_day: True for date-only planned/due days.
            priority: Task priority; use medium when unspecified.
            assignee_ids: Comma-separated assignee user IDs.
            recurrence_rrule: Recurrence rule when the task repeats.
            recurrence_timezone: Recurrence timezone.
        """
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _create():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                normalized_start_at, normalized_end_at, normalized_all_day = (
                    _normalize_task_schedule_inputs(
                        start_at=start_at,
                        end_at=end_at,
                        due_date=due_date,
                        all_day=all_day,
                    )
                )
                service = TaskManagementService()
                task = await service.create_task(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                    title=title,
                    description=description or None,
                    priority=priority or "medium",
                    start_at=normalized_start_at,
                    end_at=normalized_end_at,
                    all_day=normalized_all_day,
                    assignee_ids=_parse_ids(assignee_ids),
                    recurrence_rrule=recurrence_rrule or None,
                    recurrence_timezone=recurrence_timezone
                    or DEFAULT_TASK_TIMEZONE,
                )
                return task
            finally:
                await session.close()

        return _json(_run_async(_create()))

    @tool
    def update_task(
        task_id: str,
        title: str = "",
        description: str = "",
        status: str = "",
        priority: str = "",
        project: str = "",
        project_id: str = "",
        due_date: str = "",
        start_at: str = "",
        end_at: str = "",
        all_day: bool = False,
        assignee_ids: str = "",
        recurrence_rrule: str = "",
        recurrence_timezone: str = "",
    ) -> str:
        """Update task details. `due_date` is for date-only 予定日/deadlines."""
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _update():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                service = TaskManagementService()
                updates = {}
                if title:
                    updates["title"] = title
                if description:
                    updates["description"] = description
                if status:
                    updates["status"] = status
                if priority:
                    updates["priority"] = priority
                if project or project_id:
                    updates["project_id"] = resolved_project_id
                if due_date or start_at or end_at:
                    normalized_start_at, normalized_end_at, normalized_all_day = (
                        _normalize_task_schedule_inputs(
                            start_at=start_at,
                            end_at=end_at,
                            due_date=due_date,
                            all_day=all_day,
                        )
                    )
                    if normalized_start_at is not None:
                        updates["start_at"] = normalized_start_at
                    if normalized_end_at is not None:
                        updates["end_at"] = normalized_end_at
                    updates["all_day"] = normalized_all_day
                if assignee_ids:
                    updates["assignee_ids"] = _parse_ids(assignee_ids)
                if recurrence_rrule:
                    updates["recurrence_rrule"] = recurrence_rrule
                if recurrence_timezone:
                    updates["recurrence_timezone"] = recurrence_timezone
                task = await service.update_task(
                    session,
                    user_id=user_id,
                    task_id=UUID(task_id),
                    updates=updates,
                )
                return task
            finally:
                await session.close()

        return _json(_run_async(_update()))

    @tool
    def delete_task(task_id: str) -> str:
        """Soft-delete a task by id."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task
        from ...services.task_management_service import TaskManagementService

        async def _delete():
            db = get_database_manager()
            session = await db.get_session()
            try:
                parsed_task_id = UUID(task_id)
                task = await session.get(Task, parsed_task_id)
                if task is None or task.deleted_at is not None:
                    raise ValueError("Task not found.")
                user_id, _ = await _resolve_actor_and_project(
                    session,
                    project_id=str(task.project_id),
                )
                await TaskManagementService().delete_task(
                    session,
                    user_id=user_id,
                    task_id=parsed_task_id,
                )
                return {"success": True, "task_id": str(parsed_task_id)}
            finally:
                await session.close()

        try:
            return _json(_run_async(_delete()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def assign_task(
        task_id: str, assignee_ids: str, project: str = "", project_id: str = ""
    ) -> str:
        """Replace task assignees. `project` accepts a UUID, slug, or project name."""
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _assign():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                updates = {"assignee_ids": _parse_ids(assignee_ids)}
                if project or project_id:
                    updates["project_id"] = resolved_project_id
                service = TaskManagementService()
                return await service.update_task(
                    session,
                    user_id=user_id,
                    task_id=UUID(task_id),
                    updates=updates,
                )
            finally:
                await session.close()

        return _json(_run_async(_assign()))

    @tool
    def schedule_task(
        task_id: str,
        due_date: str = "",
        start_at: str = "",
        end_at: str = "",
        all_day: bool = False,
        recurrence_rrule: str = "",
        recurrence_timezone: str = "",
    ) -> str:
        """Update task scheduling fields and recurrence. Use `due_date` for date-only 予定日/deadlines."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task
        from ...services.task_management_service import TaskManagementService

        async def _schedule():
            db = get_database_manager()
            session = await db.get_session()
            try:
                parsed_task_id = UUID(task_id)
                task = await session.get(Task, parsed_task_id)
                if task is None:
                    raise ValueError("Task not found.")
                user_id, _ = await _resolve_actor_and_project(
                    session,
                    project_id=str(task.project_id),
                )
                updates = {}
                if due_date or start_at or end_at:
                    normalized_start_at, normalized_end_at, normalized_all_day = (
                        _normalize_task_schedule_inputs(
                            start_at=start_at,
                            end_at=end_at,
                            due_date=due_date,
                            all_day=all_day,
                        )
                    )
                    if normalized_start_at is not None:
                        updates["start_at"] = normalized_start_at
                    if normalized_end_at is not None:
                        updates["end_at"] = normalized_end_at
                    updates["all_day"] = normalized_all_day
                if recurrence_rrule:
                    updates["recurrence_rrule"] = recurrence_rrule
                if recurrence_timezone:
                    updates["recurrence_timezone"] = recurrence_timezone
                service = TaskManagementService()
                return await service.update_task(
                    session,
                    user_id=user_id,
                    task_id=parsed_task_id,
                    updates=updates,
                )
            finally:
                await session.close()

        return _json(_run_async(_schedule()))

    return [
        list_tasks,
        create_task,
        update_task,
        delete_task,
        assign_task,
        schedule_task,
    ]
