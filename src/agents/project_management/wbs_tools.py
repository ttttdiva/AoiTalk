"""WBS・課題管理表Excelの同期と確認事項要約関連ツール。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agents import function_tool
from sqlalchemy import select

from .common import (
    _run_async,
    _resolve_actor_and_project,
    _resolve_wbs_project_context,
    _json,
    _sync_wbs_record_table,
    _sync_issue_record_table,
)


def build_wbs_tools() -> list:
    """WBS・課題管理表Excelの同期と確認事項要約関連ツールのツール群を生成して返す。"""

    @function_tool
    def get_project_issues(
        limit: int = 20,
        include_closed: bool = False,
        project: str = "",
        project_id: str = "",
        issue_file: str = "",
    ) -> str:
        """Read a project's issue tracker Excel and return issue rows. `issue_file` can override the configured file."""
        from ...memory.database import get_database_manager
        from ...services.project_issue_excel_service import (
            is_closed_issue,
            read_issue_rows,
            summarize_issue_rows,
        )

        def _compact_row(row):
            data = row.to_dict()
            data.pop("raw", None)
            return data

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                context = await _resolve_wbs_project_context(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if not context:
                    return {"error": "No active or matching project context."}
                rows, errors, resolved_issue_file = read_issue_rows(
                    context,
                    issue_file=issue_file,
                )
                filtered = rows if include_closed else [
                    row for row in rows if not is_closed_issue(row)
                ]
                filtered.sort(
                    key=lambda row: (
                        row.due_date or "9999-12-31",
                        row.importance != "高",
                        row.number or 999999,
                    )
                )
                return {
                    "project_id": context.get("id"),
                    "project_name": context.get("name"),
                    "issue_file": resolved_issue_file or context.get("issue_file"),
                    "errors": errors,
                    "summary": summarize_issue_rows(rows),
                    "issues": [_compact_row(row) for row in filtered[:limit]],
                }
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @function_tool
    def sync_issue_table(
        dry_run: bool = True,
        project: str = "",
        project_id: str = "",
        issue_file: str = "",
    ) -> str:
        """Create/update 課題管理表.dbtable rows from a project's issue tracker Excel. Defaults to dry-run preview."""
        from ...memory.database import get_database_manager
        from ...services.project_issue_excel_service import (
            read_issue_rows,
            summarize_issue_rows,
        )

        async def _sync():
            db = get_database_manager()
            session = await db.get_session()
            try:
                context = await _resolve_wbs_project_context(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if not context or not context.get("id"):
                    return {"error": "No active or matching project context."}
                rows, errors, resolved_issue_file = read_issue_rows(
                    context,
                    issue_file=issue_file,
                )
                if errors and not rows:
                    return {
                        "dry_run": dry_run,
                        "project_id": str(context.get("id")),
                        "project_name": context.get("name"),
                        "issue_file": resolved_issue_file or context.get("issue_file"),
                        "errors": errors,
                        "scanned": 0,
                        "summary": summarize_issue_rows(rows),
                        "record_sync": {
                            "skipped": True,
                            "reason": "Issue rows were not readable.",
                        },
                    }
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session, project_id=str(context["id"])
                )
                record_sync = await _sync_issue_record_table(
                    session,
                    resolved_project_id,
                    user_id,
                    rows,
                    dry_run,
                    resolved_issue_file,
                )
                if not dry_run:
                    await session.commit()
                return {
                    "dry_run": dry_run,
                    "project_id": str(resolved_project_id),
                    "project_name": context.get("name"),
                    "issue_file": resolved_issue_file or context.get("issue_file"),
                    "errors": errors,
                    "scanned": len(rows),
                    "summary": summarize_issue_rows(rows),
                    "record_sync": record_sync,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        return _json(_run_async(_sync()))

    @function_tool
    def get_upcoming_wbs_tasks(
        limit: int = 10,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Read a project's WBS Excel and return upcoming unfinished rows. `project` accepts a UUID, slug, or project name."""
        from ...memory.database import get_database_manager
        from ...services.wbs_excel_service import read_wbs_rows

        def _sort_key(row):
            return (
                row.planned_end or "9999-12-31",
                row.priority not in {"urgent", "high"},
                row.title,
            )

        def _compact_row(row):
            data = row.to_dict()
            data.pop("raw", None)
            return data

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                context = await _resolve_wbs_project_context(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if not context:
                    return {"error": "No active or matching project context."}
                rows, errors = read_wbs_rows(context)
                unfinished = [row for row in rows if row.status != "closed"]
                return {
                    "project_id": context.get("id"),
                    "project_name": context.get("name"),
                    "wbs_file": context.get("wbs_file"),
                    "errors": errors,
                    "total": len(rows),
                    "tasks": [
                        _compact_row(row)
                        for row in sorted(unfinished, key=_sort_key)[:limit]
                    ],
                }
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @function_tool
    def summarize_project_requests(
        limit: int = 20,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Find customer/internal confirmation items from a project's WBS."""
        from ...memory.database import get_database_manager
        from ...services.wbs_excel_service import read_wbs_rows, summarize_request_items

        async def _summarize():
            db = get_database_manager()
            session = await db.get_session()
            try:
                context = await _resolve_wbs_project_context(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if not context:
                    return {"error": "No active or matching project context."}
                rows, errors = read_wbs_rows(context)
                return {
                    "project_id": context.get("id"),
                    "project_name": context.get("name"),
                    "wbs_file": context.get("wbs_file"),
                    "errors": errors,
                    "requests": summarize_request_items(rows, limit=limit),
                }
            finally:
                await session.close()

        return _json(_run_async(_summarize()))

    @function_tool
    def sync_wbs_tasks(
        dry_run: bool = True,
        sync_tasks: bool = False,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Create/update WBS.dbtable rows from a project's WBS. Set sync_tasks=true only when explicitly asked to mirror WBS rows into normal tasks."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task
        from ...services.task_management_service import TaskManagementService
        from ...services.wbs_excel_service import read_wbs_rows

        def _to_datetime(value: str | None) -> datetime | None:
            if not value:
                return None
            return datetime.fromisoformat(f"{value}T00:00:00")

        def _task_window(row) -> tuple[datetime | None, datetime | None]:
            start_at = _to_datetime(row.planned_start or row.actual_start)
            end_at = _to_datetime(row.planned_end or row.actual_end)
            if start_at is not None and end_at is not None and end_at <= start_at:
                end_at = end_at + timedelta(days=1)
            return start_at, end_at

        def _task_metadata(row) -> dict[str, Any]:
            return {
                "source": "wbs",
                "wbs": {
                    "source_key": row.source_key,
                    "row_hash": row.row_hash,
                    "file_path": row.file_path,
                    "sheet_name": row.sheet_name,
                    "row_number": row.row_number,
                    "wbs_id": row.wbs_id,
                    "assignee": row.assignee,
                    "progress": row.progress,
                    "request_text": row.request_text,
                    "last_synced_at": datetime.utcnow().isoformat(),
                },
            }

        async def _sync():
            db = get_database_manager()
            session = await db.get_session()
            try:
                context = await _resolve_wbs_project_context(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if not context or not context.get("id"):
                    return {"error": "No active or matching project context."}
                rows, errors = read_wbs_rows(context)
                if errors and not rows:
                    return {
                        "dry_run": dry_run,
                        "project_id": str(context.get("id")),
                        "project_name": context.get("name"),
                        "wbs_file": context.get("wbs_file"),
                        "errors": errors,
                        "scanned": 0,
                        "created_count": 0,
                        "updated_count": 0,
                        "unchanged_count": 0,
                        "created": [],
                        "updated": [],
                        "sync_tasks": sync_tasks,
                        "task_sync": {"skipped": True, "reason": "WBS rows were not readable."},
                        "record_sync": {"skipped": True, "reason": "WBS rows were not readable."},
                    }
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session, project_id=str(context["id"])
                )
                record_sync = await _sync_wbs_record_table(
                    session,
                    resolved_project_id,
                    user_id,
                    rows,
                    dry_run,
                )
                if not sync_tasks:
                    if not dry_run:
                        await session.commit()
                    return {
                        "dry_run": dry_run,
                        "sync_tasks": False,
                        "project_id": str(resolved_project_id),
                        "project_name": context.get("name"),
                        "wbs_file": context.get("wbs_file"),
                        "errors": errors,
                        "scanned": len(rows),
                        "created_count": 0,
                        "updated_count": 0,
                        "unchanged_count": 0,
                        "created": [],
                        "updated": [],
                        "task_sync": {
                            "skipped": True,
                            "reason": "sync_tasks=false; WBS rows were synced only to WBS.dbtable.",
                        },
                        "record_sync": record_sync,
                    }
                result = await session.execute(
                    select(Task).where(
                        Task.project_id == resolved_project_id,
                        Task.source == "wbs",
                        Task.deleted_at.is_(None),
                    )
                )
                existing = {}
                for task in result.scalars().all():
                    metadata = task.task_metadata or {}
                    wbs = metadata.get("wbs") if isinstance(metadata.get("wbs"), dict) else {}
                    source_key = wbs.get("source_key")
                    if source_key:
                        existing[source_key] = task

                service = TaskManagementService()
                created = []
                updated = []
                unchanged = []
                for row in rows:
                    start_at, end_at = _task_window(row)
                    existing_task = existing.get(row.source_key)
                    if existing_task is not None:
                        metadata = existing_task.task_metadata or {}
                        wbs = metadata.get("wbs") if isinstance(metadata.get("wbs"), dict) else {}
                        if wbs.get("row_hash") == row.row_hash:
                            unchanged.append(row.title)
                            continue
                        updated.append(row.title)
                        if dry_run:
                            continue
                        existing_task.title = row.title
                        existing_task.description = row.description
                        existing_task.status = row.status
                        existing_task.priority = row.priority
                        existing_task.start_at = start_at
                        existing_task.end_at = end_at
                        existing_task.all_day = True
                        existing_task.task_metadata = {
                            **metadata,
                            **_task_metadata(row),
                            "wbs": {
                                **(wbs if isinstance(wbs, dict) else {}),
                                **_task_metadata(row)["wbs"],
                            },
                        }
                        existing_task.completed_at = (
                            datetime.utcnow() if row.status == "closed" else None
                        )
                        existing_task.updated_at = datetime.utcnow()
                        continue

                    created.append(row.title)
                    if dry_run:
                        continue
                    await service.create_task(
                        session,
                        user_id=user_id,
                        project_id=resolved_project_id,
                        title=row.title,
                        description=row.description,
                        status=row.status,
                        priority=row.priority,
                        start_at=start_at,
                        end_at=end_at,
                        all_day=True,
                        assignee_ids=[user_id],
                        source="wbs",
                        task_metadata=_task_metadata(row),
                    )
                if not dry_run:
                    await session.commit()
                return {
                    "dry_run": dry_run,
                    "sync_tasks": True,
                    "project_id": str(resolved_project_id),
                    "project_name": context.get("name"),
                    "wbs_file": context.get("wbs_file"),
                    "errors": errors,
                    "scanned": len(rows),
                    "created_count": len(created),
                    "updated_count": len(updated),
                    "unchanged_count": len(unchanged),
                    "created": created[:50],
                    "updated": updated[:50],
                    "record_sync": record_sync,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        return _json(_run_async(_sync()))

    return [
        get_project_issues,
        sync_issue_table,
        get_upcoming_wbs_tasks,
        summarize_project_requests,
        sync_wbs_tasks,
    ]
