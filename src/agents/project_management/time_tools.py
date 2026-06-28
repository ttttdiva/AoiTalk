"""タイマー・時間記録・カレンダー・レポート関連ツール。"""

from __future__ import annotations

from uuid import UUID

from ...tools.core import tool

from .common import (
    _run_async,
    _resolve_actor_and_project,
    _parse_datetime,
    _json,
)


def build_time_tools() -> list:
    """タイマー・時間記録・カレンダー・レポート関連ツールのツール群を生成して返す。"""

    @tool
    def start_timer(task_id: str, occurrence_id: str = "") -> str:
        """Start a timer for a task and auto-stop any existing active timer."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task
        from ...services.task_management_service import TaskManagementService

        async def _start():
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
                service = TaskManagementService()
                return await service.start_timer(
                    session,
                    user_id=user_id,
                    task_id=parsed_task_id,
                    occurrence_id=UUID(occurrence_id) if occurrence_id else None,
                    source="agent",
                )
            finally:
                await session.close()

        return _json(_run_async(_start()))

    @tool
    def stop_timer(time_entry_id: str = "") -> str:
        """Stop the active timer or a specific active time entry."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task, TimeEntry
        from ...services.task_management_service import TaskManagementService

        async def _stop():
            db = get_database_manager()
            session = await db.get_session()
            try:
                parsed_time_entry_id = UUID(time_entry_id) if time_entry_id else None
                project_id_for_actor = ""
                if parsed_time_entry_id:
                    entry = await session.get(TimeEntry, parsed_time_entry_id)
                    if entry is not None:
                        task = await session.get(Task, entry.task_id)
                        if task is not None:
                            project_id_for_actor = str(task.project_id)
                user_id, _ = await _resolve_actor_and_project(
                    session,
                    project_id=project_id_for_actor,
                )
                service = TaskManagementService()
                return await service.stop_timer(
                    session,
                    user_id=user_id,
                    time_entry_id=parsed_time_entry_id,
                )
            finally:
                await session.close()

        return _json(_run_async(_stop()))

    @tool
    def log_time(
        task_id: str,
        started_at: str,
        ended_at: str,
        occurrence_id: str = "",
        note: str = "",
    ) -> str:
        """Create a completed manual time entry for a task."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task
        from ...services.task_management_service import TaskManagementService

        async def _log():
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
                service = TaskManagementService()
                return await service.log_time(
                    session,
                    user_id=user_id,
                    task_id=parsed_task_id,
                    occurrence_id=UUID(occurrence_id) if occurrence_id else None,
                    started_at=_parse_datetime(started_at),
                    ended_at=_parse_datetime(ended_at),
                    source="agent",
                    note=note or None,
                )
            finally:
                await session.close()

        return _json(_run_async(_log()))

    @tool
    def list_calendar(
        project: str = "",
        project_id: str = "",
        start_from: str = "",
        end_to: str = "",
    ) -> str:
        """List calendar occurrences. `project` accepts a UUID, slug, or project name."""
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
                return await service.list_occurrences(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                    start_from=_parse_datetime(start_from),
                    end_to=_parse_datetime(end_to),
                )
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @tool
    def get_time_report(
        project: str = "",
        project_id: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> str:
        """Get tracked-time reporting. `project` accepts a UUID, slug, or project name."""
        from ...memory.database import get_database_manager
        from ...services.task_management_service import TaskManagementService

        async def _report():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                service = TaskManagementService()
                return await service.get_time_report(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                    date_from=_parse_datetime(date_from),
                    date_to=_parse_datetime(date_to),
                )
            finally:
                await session.close()

        return _json(_run_async(_report()))

    return [
        start_timer,
        stop_timer,
        log_time,
        list_calendar,
        get_time_report,
    ]
