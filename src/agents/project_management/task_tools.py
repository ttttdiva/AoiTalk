"""タスクのCRUD・割り当て・スケジュール関連ツール。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from ...tools.core import tool
from ...services.agent_run_service import get_current_agent_run_id

from ...task_time import DEFAULT_TASK_TIMEZONE
from .common import (
    _run_async,
    _normalize_task_schedule_inputs,
    _resolve_actor_and_project,
    _parse_ids,
    _json,
)


_PARENT_TASK_ID_UNSET = object()


def build_task_tools() -> list:
    """タスクのCRUD・割り当て・スケジュール関連ツールのツール群を生成して返す。"""

    @tool
    def list_tasks(
        project: str = "", project_id: str = "", status: str = "", search: str = ""
    ) -> str:
        """List project tasks, including each task's `parent_task_id` hierarchy.

        Use this before creating tasks so related work can reuse an existing
        parent instead of creating duplicate top-level containers. Prefer
        ``search_task_candidates`` for duplicate checks because it returns a
        bounded lightweight list; use ``get_task`` for full detail on one id.

        Args:
            project: Project UUID, slug, or name. Omit it to use the current project or Inbox.
            project_id: Explicit project UUID. Omit it to use the current project or Inbox.
            status: Optional task status filter.
            search: Optional task title/description search used to find related candidates.
        """
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
    def search_task_candidates(
        project: str = "",
        project_id: str = "",
        status: str = "",
        search: str = "",
        limit: int = 25,
    ) -> str:
        """Search lightweight task candidates before creating or updating tasks.

        Returns bounded id/title/status/project/parent/updated_at/snippet rows.
        Use ``get_task`` when one candidate needs full detail.

        Args:
            project: Project UUID, slug, or name. Omit it to use the current project or Inbox.
            project_id: Explicit project UUID. Omit it to use the current project or Inbox.
            status: Optional task status filter.
            search: Optional title/description search used to find related candidates.
            limit: Maximum candidates to return (1-50, default 25).
        """
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _search():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                service = TaskManagementService()
                return await service.search_task_candidates(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                    status=status or None,
                    search=search or None,
                    limit=limit,
                )
            finally:
                await session.close()

        return _json(_run_async(_search()))

    @tool
    def get_task(task_id: str, project: str = "", project_id: str = "") -> str:
        """Load one task in full after a candidate id is chosen."""
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _get():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, _ = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                service = TaskManagementService()
                return await service.get_task(
                    session,
                    user_id=user_id,
                    task_id=UUID(task_id),
                )
            finally:
                await session.close()

        return _json(_run_async(_get()))

    @tool
    def create_task(
        title: str,
        description: str = "",
        project: str = "",
        project_id: str = "",
        parent_task_id: Optional[str] = None,
        due_date: str = "",
        start_at: str = "",
        end_at: str = "",
        all_day: bool = False,
        auto_close_on_due: bool = False,
        priority: str = "medium",
        assignee_ids: str = "",
        recurrence_rrule: str = "",
        recurrence_timezone: str = DEFAULT_TASK_TIMEZONE,
    ) -> str:
        """Create one task, optionally as a child of an existing task.

        Inspect the project's existing tasks first. Set `parent_task_id` only
        when the parent clearly contains this work; cross-cutting relationships
        are not parent/child containment. The service validates the parent and
        project invariant.

        Args:
            title: Concise user-facing task title. Infer it from the concrete action/event. For reservation emails, use only venue/service + purpose such as "予約先 来店（サービス名）"; do not include appointment dates/times, parenthesized dates/times, generic labels like "予約確認タスク", or reservation numbers as the main title.
            description: Supporting details such as reservation number, date/time, price, coupon/point usage, contact information, cancellation notes, and the source email facts.
            project: UUID, slug, or project name. Omit it to use the current project or Inbox.
            project_id: Explicit project UUID. Omit it to use the current project or Inbox.
            parent_task_id: Existing parent task UUID when this is an actionable subtask. Omit it for an independent top-level deliverable.
            due_date: Date-only planned/due day in YYYY-MM-DD format when the task has a 予定日, deadline, or appointment day but no specific time. Use this instead of only mentioning the date in the response.
            start_at: Task start datetime when the content contains an appointment or scheduled work time.
            end_at: Task end datetime when known.
            all_day: True for date-only planned/due days.
            auto_close_on_due: Automatically close the task after its due time (opt-in; defaults to False).
            priority: Task priority; use medium when unspecified.
            assignee_ids: Comma-separated assignee user IDs.
            recurrence_rrule: Recurrence rule when the task repeats.
            recurrence_timezone: Recurrence timezone.
        """
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        # _run_asyncは別スレッドを使うため、境界を越える前に明示的に捕捉する。
        agent_run_id = get_current_agent_run_id()

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
                    parent_task_id=UUID(parent_task_id) if parent_task_id else None,
                    title=title,
                    description=description or None,
                    priority=priority or "medium",
                    start_at=normalized_start_at,
                    end_at=normalized_end_at,
                    all_day=normalized_all_day,
                    auto_close_on_due=auto_close_on_due,
                    assignee_ids=_parse_ids(assignee_ids),
                    recurrence_rrule=recurrence_rrule or None,
                    recurrence_timezone=recurrence_timezone
                    or DEFAULT_TASK_TIMEZONE,
                    agent_run_id=agent_run_id,
                )
                if agent_run_id:
                    from sqlalchemy import select
                    from sqlalchemy.exc import IntegrityError

                    from ...memory.models import AgentRun, TaskAppLink

                    run = await session.get(AgentRun, UUID(str(agent_run_id)))
                    if run is not None and run.app_id is not None:
                        normalized_text = f"{title} {description}".casefold()
                        if any(marker in normalized_text for marker in ("fix", "bug", "修正", "不具合", "障害")):
                            relation_type = "fixes"
                        elif any(marker in normalized_text for marker in ("test", "検証", "テスト", "確認")):
                            relation_type = "tests"
                        elif any(marker in normalized_text for marker in ("release", "リリース", "公開", "配布")):
                            relation_type = "releases"
                        elif any(marker in normalized_text for marker in ("use", "利用", "運用", "実行")):
                            relation_type = "uses"
                        elif any(marker in normalized_text for marker in ("related", "関連", "参照", "連携")):
                            relation_type = "related"
                        else:
                            relation_type = "develops"
                        link_query = select(TaskAppLink).where(
                            TaskAppLink.task_id == UUID(str(task["id"])),
                            TaskAppLink.app_id == run.app_id,
                            TaskAppLink.relation_type == relation_type,
                        )
                        link_query = link_query.where(
                            TaskAppLink.target_id.is_(None)
                            if run.app_target_id is None
                            else TaskAppLink.target_id == run.app_target_id
                        )
                        if await session.scalar(link_query) is None:
                            session.add(TaskAppLink(
                                task_id=UUID(str(task["id"])),
                                app_id=run.app_id,
                                target_id=run.app_target_id,
                                relation_type=relation_type,
                                created_by=user_id,
                            ))
                            try:
                                await session.commit()
                            except IntegrityError:
                                # A retry may have inserted the same formal
                                # TaskAppLink already.  The Task itself was
                                # committed by TaskManagementService, so
                                # rollback only the link transaction and
                                # return the durable winner.
                                await session.rollback()
                                if await session.scalar(link_query) is None:
                                    raise
                        task["app_id"] = str(run.app_id)
                        task["app_target_id"] = str(run.app_target_id) if run.app_target_id else None
                        task["app_relation_type"] = relation_type
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
        parent_task_id: Optional[str] = _PARENT_TASK_ID_UNSET,
        due_date: str = "",
        start_at: str = "",
        end_at: str = "",
        all_day: bool = False,
        auto_close_on_due: Optional[bool] = None,
        assignee_ids: str = "",
        recurrence_rrule: str = "",
        recurrence_timezone: str = "",
        close_incomplete_subtasks: bool = False,
    ) -> str:
        """Update task details, including its hierarchy placement.

        `due_date` is for date-only 予定日/deadlines. Closing a task with
        incomplete direct subtasks requires `close_incomplete_subtasks=True`;
        this explicit flag closes those subtasks in the same transaction.
        `auto_close_on_due` enables/disables due-date auto completion; omit it
        to leave the existing setting unchanged. Omit `parent_task_id` to keep
        the current parent, pass a parent UUID to reparent, or pass an empty string
        to make the task top-level. The service validates cycles and project
        membership.

        Args:
            parent_task_id: Omit to preserve the current parent; UUID to reparent; empty string to detach to top level.
        """
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
                if parent_task_id is not _PARENT_TASK_ID_UNSET:
                    updates["parent_task_id"] = (
                        UUID(parent_task_id) if parent_task_id else None
                    )
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
                if auto_close_on_due is not None:
                    updates["auto_close_on_due"] = auto_close_on_due
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
                    close_incomplete_subtasks=close_incomplete_subtasks,
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
                task_title = task.title
                task_start_at = task.start_at
                task_end_at = task.end_at
                task_all_day = task.all_day
                user_id, _ = await _resolve_actor_and_project(
                    session,
                    project_id=str(task.project_id),
                )
                await TaskManagementService().delete_task(
                    session,
                    user_id=user_id,
                    task_id=parsed_task_id,
                )
                return {
                    "success": True,
                    "task_id": str(parsed_task_id),
                    "title": task_title,
                    "start_at": task_start_at,
                    "end_at": task_end_at,
                    "all_day": task_all_day,
                }
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
        auto_close_on_due: Optional[bool] = None,
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
                if task is None or task.deleted_at is not None:
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
                if auto_close_on_due is not None:
                    updates["auto_close_on_due"] = auto_close_on_due
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
        search_task_candidates,
        get_task,
        create_task,
        update_task,
        delete_task,
        assign_task,
        schedule_task,
    ]
