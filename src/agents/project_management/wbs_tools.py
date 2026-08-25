"""WBS台帳・外部WBS Excel・課題管理表Excelの同期と確認事項要約関連ツール。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from ...tools.core import tool
from sqlalchemy import select

from .common import (
    _run_async,
    _resolve_actor_and_project,
    _resolve_wbs_project_context,
    _json,
    _sync_wbs_record_table,
    _sync_issue_record_table,
)

WBS_TABLE_NAME = "WBS"

WBS_FIELD_ALIASES: dict[str, set[str]] = {
    "title": {"title", "タスク名", "作業名", "項目", "件名", "task", "name"},
    "wbs_id": {"wbs_id", "wbs id", "wbs", "wbs番号", "番号", "id"},
    "status": {"status", "状態", "ステータス"},
    "priority": {"priority", "優先度", "重要度"},
    "planned_start": {"planned_start", "planned start", "予定開始", "予定開始日", "開始予定", "開始日"},
    "planned_end": {"planned_end", "planned end", "予定終了", "予定終了日", "終了予定", "期限", "期日", "終了日"},
    "actual_start": {"actual_start", "actual start", "実績開始", "実績開始日", "着手日"},
    "actual_end": {"actual_end", "actual end", "実績終了", "実績終了日", "完了日"},
    "assignee": {"assignee", "担当", "担当者", "owner"},
    "progress": {"progress", "進捗", "進捗率", "%"},
    "request_text": {"request_text", "request", "確認事項", "要確認", "依頼事項", "顧客確認"},
}

CLOSED_WBS_STATUSES = {"closed", "done", "complete", "completed", "完了", "終了", "済"}
CLOSED_TASK_STATUSES = {"closed", "done", "complete", "completed", "完了", "終了", "済"}
GOAL_TEXT_MARKERS = (
    "目標",
    "ゴール",
    "成功条件",
    "完了条件",
    "成果物",
    "マイルストーン",
    "スコープ",
    "要件",
)


def _normalize_lookup_text(value: Any) -> str:
    return str(value or "").strip().casefold().replace("_", " ")


def _coerce_date_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _coerce_progress_fraction(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        percent = text.endswith("%")
        text = text[:-1].strip() if percent else text
        try:
            number = float(text)
        except ValueError:
            return None
        return max(0.0, min(1.0, number / 100 if percent or number > 1 else number))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number / 100 if number > 1 else number))


def _record_value(
    values: dict[str, Any],
    fields_by_key: dict[str, Any],
    canonical_key: str,
) -> Any:
    if canonical_key in values:
        return values[canonical_key]
    aliases = {_normalize_lookup_text(item) for item in WBS_FIELD_ALIASES[canonical_key]}
    for key, value in values.items():
        if _normalize_lookup_text(key) in aliases:
            return value
    for field_key, field in fields_by_key.items():
        if field_key not in values:
            continue
        if _normalize_lookup_text(getattr(field, "label", "")) in aliases:
            return values[field_key]
    return None


def _normalize_internal_wbs_status(
    status: Any,
    progress: float | None,
    actual_end: str | None,
) -> str:
    text = str(status or "").strip()
    lowered = text.casefold()
    if actual_end or (progress is not None and progress >= 1):
        return "closed"
    if not text:
        return "open"
    if any(term in lowered for term in ("完了", "終了", "done", "closed", "complete")):
        return "closed"
    if any(term in lowered for term in ("確認", "review", "waiting")):
        return "review"
    if any(term in lowered for term in ("保留", "blocked", "block")):
        return "blocked"
    if any(term in lowered for term in ("進行", "対応中", "着手", "doing", "progress")):
        return "in_progress"
    if any(term in lowered for term in ("未着手", "todo", "open", "not started")):
        return "open"
    return text


def _internal_wbs_record_to_dict(row: Any, fields: list[Any]) -> dict[str, Any]:
    values = row.values if isinstance(getattr(row, "values", None), dict) else {}
    fields_by_key = {str(field.key): field for field in fields}
    progress = _coerce_progress_fraction(_record_value(values, fields_by_key, "progress"))
    actual_end = _coerce_date_string(_record_value(values, fields_by_key, "actual_end"))
    status = _normalize_internal_wbs_status(
        _record_value(values, fields_by_key, "status") or getattr(row, "status", None),
        progress,
        actual_end,
    )
    metadata = row.row_metadata if isinstance(getattr(row, "row_metadata", None), dict) else {}
    return {
        "source": "internal_db",
        "record_row_id": str(row.id),
        "source_key": metadata.get("source_key") or f"record_row:{row.id}",
        "row_hash": metadata.get("row_hash"),
        "file_path": metadata.get("file_path"),
        "sheet_name": metadata.get("sheet_name"),
        "row_number": metadata.get("row_number"),
        "wbs_id": _record_value(values, fields_by_key, "wbs_id"),
        "title": (
            _record_value(values, fields_by_key, "title")
            or getattr(row, "title", None)
            or "(untitled)"
        ),
        "description": None,
        "assignee": _record_value(values, fields_by_key, "assignee"),
        "status": status,
        "priority": _record_value(values, fields_by_key, "priority") or "medium",
        "planned_start": _coerce_date_string(
            _record_value(values, fields_by_key, "planned_start")
        ),
        "planned_end": (
            _coerce_date_string(_record_value(values, fields_by_key, "planned_end"))
            or _coerce_date_string(getattr(row, "due_at", None))
        ),
        "actual_start": _coerce_date_string(
            _record_value(values, fields_by_key, "actual_start")
        ),
        "actual_end": actual_end,
        "progress": progress,
        "request_text": _record_value(values, fields_by_key, "request_text"),
    }


def _internal_wbs_row_is_closed(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").casefold()
    return status in CLOSED_WBS_STATUSES


def _summarize_internal_wbs_request_items(
    rows: list[dict[str, Any]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        request_text = str(row.get("request_text") or "").strip()
        if not request_text:
            continue
        target = "customer" if any(term in request_text for term in ("顧客", "お客様", "客先")) else "internal"
        items.append(
            {
                "target": target,
                "title": request_text,
                "related_task": row.get("title"),
                "planned_end": row.get("planned_end"),
                "assignee": row.get("assignee"),
                "source": row.get("source"),
                "record_row_id": row.get("record_row_id"),
            }
        )
        if len(items) >= limit:
            break
    return items


async def _read_internal_wbs_rows(
    session,
    project_id: UUID,
) -> dict[str, Any]:
    from ...memory.models import RecordField, RecordRow, RecordTable

    table_result = await session.execute(
        select(RecordTable)
        .where(
            RecordTable.project_id == project_id,
            RecordTable.name == WBS_TABLE_NAME,
            RecordTable.deleted_at.is_(None),
        )
        .limit(1)
    )
    table = table_result.scalar_one_or_none()
    if table is None:
        return {"table_id": None, "rows": []}

    fields_result = await session.execute(
        select(RecordField)
        .where(
            RecordField.table_id == table.id,
            RecordField.deleted_at.is_(None),
        )
        .order_by(RecordField.sort_order, RecordField.created_at)
    )
    rows_result = await session.execute(
        select(RecordRow)
        .where(
            RecordRow.table_id == table.id,
            RecordRow.deleted_at.is_(None),
        )
        .order_by(RecordRow.due_at.is_(None), RecordRow.due_at, RecordRow.updated_at.desc())
    )
    fields = list(fields_result.scalars().all())
    rows = [
        _internal_wbs_record_to_dict(row, fields)
        for row in rows_result.scalars().all()
    ]
    return {"table_id": str(table.id), "rows": rows}


def _sort_wbs_row_key(row: dict[str, Any]) -> tuple[Any, bool, str]:
    return (
        row.get("planned_end") or "9999-12-31",
        str(row.get("priority") or "").casefold() not in {"urgent", "high", "高"},
        str(row.get("title") or ""),
    )


def _compact_project_doc(node: Any) -> dict[str, Any]:
    body = str(getattr(node, "body_text", "") or "").strip()
    return {
        "id": str(node.id),
        "title": getattr(node, "title", None),
        "body_text": body[:500],
        "updated_at": (
            node.updated_at.isoformat()
            if getattr(node, "updated_at", None)
            else None
        ),
    }


def _doc_looks_goal_related(node: Any) -> bool:
    text = f"{getattr(node, 'title', '')} {getattr(node, 'body_text', '')}"
    return any(marker in text for marker in GOAL_TEXT_MARKERS)


def _summarize_tasks_for_progress(tasks: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    closed_count = sum(
        count
        for status, count in by_status.items()
        if status.casefold() in CLOSED_TASK_STATUSES
    )
    total = len(tasks)
    return {
        "total": total,
        "closed": closed_count,
        "active": max(0, total - closed_count),
        "completion_rate": round(closed_count / total, 3) if total else None,
        "by_status": by_status,
        "items": [
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "priority": task.get("priority"),
                "start_at": task.get("start_at"),
                "end_at": task.get("end_at"),
                "parent_task_id": task.get("parent_task_id"),
            }
            for task in tasks[:limit]
        ],
    }


def _summarize_wbs_for_progress(
    rows: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    closed_count = sum(1 for row in rows if _internal_wbs_row_is_closed(row))
    total = len(rows)
    return {
        "source": "internal_db",
        "total": total,
        "closed": closed_count,
        "active": max(0, total - closed_count),
        "completion_rate": round(closed_count / total, 3) if total else None,
        "empty_is_blocker": False,
        "items": sorted(rows, key=_sort_wbs_row_key)[:limit],
    }


def build_wbs_tools() -> list:
    """WBS台帳・外部WBS Excel・課題管理表Excel関連ツール群を生成して返す。"""

    @tool
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

    @tool
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
                    permission="write" if not dry_run else "read",
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
                    session,
                    project_id=str(context["id"]),
                    permission="write" if not dry_run else "read",
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

    @tool
    def get_project_progress(
        limit: int = 20,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Summarize project progress toward goals using Docs, internal WBS.dbtable, and built-in tasks. Empty WBS.dbtable is not a blocker."""
        from ...memory.database import get_database_manager
        from ...memory.models import KnowledgeNode, ProjectContextPack
        from ...services.task_management_service import TaskManagementService

        async def _summarize():
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
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project_id=str(context["id"]),
                )

                internal = await _read_internal_wbs_rows(session, resolved_project_id)
                tasks = await TaskManagementService().list_tasks(
                    session,
                    user_id=user_id,
                    project_id=resolved_project_id,
                )
                docs_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.project_id == resolved_project_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(50)
                )
                context_pack = await session.scalar(
                    select(ProjectContextPack).where(
                        ProjectContextPack.project_id == resolved_project_id
                    )
                )
                docs_nodes = list(docs_result.scalars().all())
                goal_docs = [
                    _compact_project_doc(node)
                    for node in docs_nodes
                    if _doc_looks_goal_related(node)
                ][:limit]
                docs_evidence = [
                    _compact_project_doc(node)
                    for node in docs_nodes[:limit]
                ]
                context_goals = (
                    context_pack.goals
                    if context_pack is not None and isinstance(context_pack.goals, list)
                    else []
                )
                current_status = (
                    context_pack.current_status
                    if context_pack is not None
                    and isinstance(context_pack.current_status, dict)
                    else {}
                )
                wbs_summary = _summarize_wbs_for_progress(internal["rows"], limit)
                task_summary = _summarize_tasks_for_progress(tasks, limit)
                evidence_sources = {
                    "project_context_goals": bool(context_goals),
                    "project_goal_docs": bool(goal_docs),
                    "project_docs": bool(docs_evidence),
                    "internal_wbs": bool(internal["rows"]),
                    "tasks": bool(tasks),
                    "current_status": bool(current_status),
                }
                has_progress_evidence = any(evidence_sources.values())
                return {
                    "source": "project_progress_summary",
                    "progress_basis": "goals_deliverables_milestones_wbs_and_tasks",
                    "project_id": str(resolved_project_id),
                    "project_name": context.get("name"),
                    "can_assess_progress": has_progress_evidence,
                    "empty_wbs_is_blocker": False,
                    "empty_wbs_handling": (
                        "If WBS.dbtable has no rows, continue using project goals, "
                        "facts, current status, and built-in tasks. Do not stop at "
                        "'WBS is empty'."
                    ),
                    "external_wbs_file": context.get("wbs_file"),
                    "external_wbs_configured": bool(context.get("wbs_file")),
                    "evidence_sources": evidence_sources,
                    "goals": context_goals[:limit],
                    "current_status": current_status,
                    "goal_docs": goal_docs,
                    "docs": docs_evidence,
                    "wbs": {
                        "table_id": internal["table_id"],
                        **wbs_summary,
                    },
                    "tasks": task_summary,
                    "insufficient_evidence_reason": None
                    if has_progress_evidence
                    else (
                        "No project goals, facts, current status, internal WBS rows, "
                        "or built-in tasks are stored yet."
                    ),
                }
            finally:
                await session.close()

        return _json(_run_async(_summarize()))

    @tool
    def get_upcoming_wbs_tasks(
        limit: int = 10,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Read internal WBS.dbtable rows and return upcoming unfinished rows. Falls back to external WBS Excel only when the internal table has no rows."""
        from ...memory.database import get_database_manager
        from ...services.wbs_excel_service import read_wbs_rows

        def _compact_row(row):
            data = row.to_dict()
            data.pop("raw", None)
            data["source"] = "external_excel"
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
                resolved_project_id = UUID(str(context["id"]))
                internal = await _read_internal_wbs_rows(session, resolved_project_id)
                internal_rows = internal["rows"]
                if internal_rows:
                    unfinished = [
                        row for row in internal_rows if not _internal_wbs_row_is_closed(row)
                    ]
                    return {
                        "source": "internal_db",
                        "project_id": context.get("id"),
                        "project_name": context.get("name"),
                        "wbs_table_id": internal["table_id"],
                        "external_wbs_file": context.get("wbs_file"),
                        "external_wbs_configured": bool(context.get("wbs_file")),
                        "errors": [],
                        "total": len(internal_rows),
                        "tasks": sorted(unfinished, key=_sort_wbs_row_key)[:limit],
                    }

                rows, errors = read_wbs_rows(context)
                unfinished = [row for row in rows if row.status != "closed"]
                if not rows:
                    return {
                        "source": "internal_db",
                        "project_id": context.get("id"),
                        "project_name": context.get("name"),
                        "wbs_table_id": internal["table_id"],
                        "external_wbs_file": context.get("wbs_file"),
                        "external_wbs_configured": bool(context.get("wbs_file")),
                        "errors": [],
                        "external_errors": errors,
                        "total": 0,
                        "tasks": [],
                        "note": (
                            "No internal WBS rows are stored yet. External WBS "
                            "Excel is optional and can be imported when available."
                        ),
                    }
                return {
                    "source": "external_excel_fallback",
                    "project_id": context.get("id"),
                    "project_name": context.get("name"),
                    "wbs_file": context.get("wbs_file"),
                    "errors": errors,
                    "total": len(rows),
                    "tasks": [
                        _compact_row(row)
                        for row in sorted(
                            unfinished,
                            key=lambda row: (
                                row.planned_end or "9999-12-31",
                                row.priority not in {"urgent", "high"},
                                row.title,
                            ),
                        )[:limit]
                    ],
                }
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @tool
    def summarize_project_requests(
        limit: int = 20,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Find customer/internal confirmation items from internal WBS.dbtable, falling back to external WBS Excel when needed."""
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
                resolved_project_id = UUID(str(context["id"]))
                internal = await _read_internal_wbs_rows(session, resolved_project_id)
                internal_rows = internal["rows"]
                if internal_rows:
                    return {
                        "source": "internal_db",
                        "project_id": context.get("id"),
                        "project_name": context.get("name"),
                        "wbs_table_id": internal["table_id"],
                        "external_wbs_file": context.get("wbs_file"),
                        "external_wbs_configured": bool(context.get("wbs_file")),
                        "errors": [],
                        "requests": _summarize_internal_wbs_request_items(
                            internal_rows,
                            limit=limit,
                        ),
                    }
                rows, errors = read_wbs_rows(context)
                if not rows:
                    return {
                        "source": "internal_db",
                        "project_id": context.get("id"),
                        "project_name": context.get("name"),
                        "wbs_table_id": internal["table_id"],
                        "external_wbs_file": context.get("wbs_file"),
                        "external_wbs_configured": bool(context.get("wbs_file")),
                        "errors": [],
                        "external_errors": errors,
                        "requests": [],
                    }
                return {
                    "source": "external_excel_fallback",
                    "project_id": context.get("id"),
                    "project_name": context.get("name"),
                    "wbs_file": context.get("wbs_file"),
                    "errors": errors,
                    "requests": summarize_request_items(rows, limit=limit),
                }
            finally:
                await session.close()

        return _json(_run_async(_summarize()))

    @tool
    def sync_wbs_tasks(
        dry_run: bool = True,
        sync_tasks: bool = False,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Import external WBS Excel rows into the internal WBS.dbtable. Set sync_tasks=true only when explicitly asked to mirror imported rows into normal tasks."""
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
                    permission="write" if not dry_run else "read",
                )
                if not context or not context.get("id"):
                    return {"error": "No active or matching project context."}
                rows, errors = read_wbs_rows(context)
                if errors and not rows:
                    return {
                        "canonical_source": "WBS.dbtable",
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
                        "task_sync": {
                            "skipped": True,
                            "reason": "External WBS Excel rows were not readable.",
                        },
                        "record_sync": {
                            "skipped": True,
                            "reason": (
                                "External WBS Excel rows were not readable. "
                                "Internal WBS.dbtable can still be edited directly."
                            ),
                        },
                    }
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project_id=str(context["id"]),
                    permission="write" if not dry_run else "read",
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
                        "canonical_source": "WBS.dbtable",
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
                            "reason": (
                                "sync_tasks=false; imported WBS rows were synced "
                                "only to the internal WBS.dbtable."
                            ),
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
                    "canonical_source": "WBS.dbtable",
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
        get_project_progress,
        get_upcoming_wbs_tasks,
        summarize_project_requests,
        sync_wbs_tasks,
    ]
