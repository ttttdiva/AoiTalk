"""project_management ツール群が共有するヘルパー関数。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select



def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


_PROJECT_REFERENCE_SUFFIXES = (
    "プロジェクト",
    "案件",
    "pj",
    "project",
)


def _clean_project_reference(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def _project_reference_variants(value: Any) -> set[str]:
    base = _clean_project_reference(value)
    variants = {base} if base else set()
    changed = True
    while changed:
        changed = False
        for item in list(variants):
            for suffix in _PROJECT_REFERENCE_SUFFIXES:
                if item.endswith(suffix):
                    stripped = item[: -len(suffix)].strip()
                    if stripped and stripped not in variants:
                        variants.add(stripped)
                        changed = True
    return variants


def _iter_project_aliases(project: dict[str, Any]) -> list[Any]:
    aliases: list[Any] = []
    for value in (project.get("aliases"),):
        if isinstance(value, list):
            aliases.extend(value)
    metadata = project.get("metadata")
    if not isinstance(metadata, dict):
        metadata = project.get("project_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = None
    if isinstance(metadata, dict):
        metadata_aliases = metadata.get("aliases")
        if isinstance(metadata_aliases, list):
            aliases.extend(metadata_aliases)
    return aliases


def _project_reference_terms(project: dict[str, Any]) -> set[str]:
    values = [
        project.get("id"),
        project.get("slug"),
        project.get("name"),
        *_iter_project_aliases(project),
    ]
    terms: set[str] = set()
    for value in values:
        terms.update(_project_reference_variants(value))
    return {term for term in terms if term}


def _project_reference_match_score(project: dict[str, Any], reference: str) -> int:
    ref_variants = _project_reference_variants(reference)
    if not ref_variants:
        return 0
    terms = _project_reference_terms(project)
    if ref_variants & terms:
        return 100
    for ref in ref_variants:
        for term in terms:
            if len(ref) >= 3 and ref in term:
                return 80
            if len(term) >= 3 and term in ref:
                return 70
    return 0


_DATE_ONLY_INPUT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_wall_clock_datetime_value(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1]
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _is_date_only_schedule_input(raw_value: str, parsed: datetime | None) -> bool:
    value = (raw_value or "").strip()
    if not value or parsed is None:
        return False
    if _DATE_ONLY_INPUT_RE.match(value):
        return True
    return (
        parsed.hour == 0
        and parsed.minute == 0
        and parsed.second == 0
        and parsed.microsecond == 0
    )


def _normalize_task_schedule_inputs(
    *,
    start_at: str = "",
    end_at: str = "",
    due_date: str = "",
    all_day: bool = False,
) -> tuple[datetime | None, datetime | None, bool]:
    """Normalize agent task schedule args into DB start/end/all-day fields."""
    source_start = (due_date or start_at or "").strip()
    parsed_start = _parse_wall_clock_datetime_value(source_start)
    parsed_end = _parse_wall_clock_datetime_value(end_at)
    date_only = _is_date_only_schedule_input(source_start, parsed_start)
    normalized_all_day = bool(all_day or due_date or date_only)

    if parsed_start and normalized_all_day and parsed_end is None:
        parsed_end = parsed_start + timedelta(days=1)

    return parsed_start, parsed_end, normalized_all_day


def _coalesce_project_reference(project: str = "", project_id: str = "") -> str:
    return (project or project_id or "").strip()


async def _resolve_operator_user_id(session) -> UUID:
    from ...memory.models import User

    admin_result = await session.execute(
        select(User).where(User.role == "admin").limit(1)
    )
    admin_user = admin_result.scalar_one_or_none()
    if admin_user:
        return admin_user.id

    first_user_result = await session.execute(select(User).limit(1))
    first_user = first_user_result.scalar_one_or_none()
    if first_user:
        return first_user.id

    raise ValueError("No local user exists to act as the task operator.")


async def _resolve_project(
    session,
    project_ref: str = "",
):
    from ...memory.models import Project
    from ...memory.project_repository import ProjectRepository
    from ...services.project_context import get_runtime_project_context

    target_project_ref = (project_ref or "").strip()
    runtime_context = get_runtime_project_context()
    if not target_project_ref and runtime_context and runtime_context.get("id"):
        target_project_ref = str(runtime_context["id"])

    operator_user_id = await _resolve_operator_user_id(session)

    if not target_project_ref:
        default_project_id = await ProjectRepository.get_user_inbox_project_id(
            session, operator_user_id
        )
        return (
            await session.get(Project, default_project_id)
            if default_project_id
            else None
        )

    try:
        parsed_project_id = UUID(target_project_ref)
    except ValueError:
        parsed_project_id = None

    if parsed_project_id is not None:
        project = await session.get(Project, parsed_project_id)
        if project:
            return project

    projects = await ProjectRepository.get_user_projects(
        session, user_id=operator_user_id
    )
    ranked_matches = [
        (score, project)
        for project in projects
        if (score := _project_reference_match_score(project, target_project_ref))
        > 0
    ]
    if ranked_matches:
        best_score = max(score for score, _ in ranked_matches)
        best_matches = [
            project for score, project in ranked_matches if score == best_score
        ]
        if len(best_matches) == 1:
            return await session.get(Project, UUID(best_matches[0]["id"]))
        raise ValueError(
            f"Project reference is ambiguous: {target_project_ref}"
        )

    fallback_result = await session.execute(
        select(Project).where(Project.deleted_at.is_(None)).limit(200)
    )
    fallback_ranked = [
        (score, project)
        for project in fallback_result.scalars().all()
        if (
            score := _project_reference_match_score(
                project.to_dict(), target_project_ref
            )
        )
        > 0
    ]
    if fallback_ranked:
        best_score = max(score for score, _ in fallback_ranked)
        fallback_projects = [
            project for score, project in fallback_ranked if score == best_score
        ]
        if len(fallback_projects) == 1:
            return fallback_projects[0]
        raise ValueError(
            f"Project reference is ambiguous: {target_project_ref}"
        )

    raise ValueError(f"Project not found: {target_project_ref}")


async def _resolve_actor_and_project(
    session,
    project: str = "",
    project_id: str = "",
) -> tuple[UUID, UUID | None]:
    operator_user_id = await _resolve_operator_user_id(session)
    resolved_project = await _resolve_project(
        session,
        _coalesce_project_reference(project, project_id),
    )
    if resolved_project:
        return (
            resolved_project.owner_id or operator_user_id,
            resolved_project.id,
        )
    return operator_user_id, None


async def _resolve_wbs_project_context(
    session,
    project: str = "",
    project_id: str = "",
) -> dict[str, Any] | None:
    from ...services.project_context import (
        build_project_context,
        get_runtime_project_context,
    )

    project_ref = _coalesce_project_reference(project, project_id)
    if project_ref:
        project_obj = await _resolve_project(session, project_ref)
        return build_project_context(project_obj) if project_obj else None

    runtime_context = get_runtime_project_context()
    if runtime_context and runtime_context.get("id"):
        return runtime_context
    return None


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_wall_clock_datetime(value: str) -> datetime | None:
    return _parse_wall_clock_datetime_value(value)


def _parse_ids(value: str) -> list[UUID]:
    text = (value or "").strip()
    if not text:
        return []
    return [UUID(part.strip()) for part in text.split(",") if part.strip()]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


record_field_types = {
    "text",
    "long_text",
    "number",
    "date",
    "select",
    "multi_select",
    "checkbox",
    "url",
    "file",
}


project_info_category_statuses = {"active", "suggested", "hidden", "archived"}


project_info_item_statuses = {"active", "suggested", "archived"}


project_info_target_kinds = {"file", "record_table", "url"}


project_info_ai_access_levels = {"metadata", "read", "edit", "blocked"}


default_project_info_categories = [
    {
        "key": "overview",
        "label": "概要",
        "description": "案件の目的・範囲・前提を置く入口カテゴリ。",
        "status": "active",
        "sort_order": 0,
    },
    {
        "key": "important_documents",
        "label": "重要資料",
        "description": "パラメーターシート、構成図、設計書などの正本資料。",
        "status": "active",
        "sort_order": 10,
    },
    {
        "key": "decisions",
        "label": "決定事項",
        "description": "顧客・社内・ベンダー間で決まったこと。",
        "status": "active",
        "sort_order": 20,
    },
    {
        "key": "open_questions",
        "label": "要確認",
        "description": "未確定事項、回答待ち、確認依頼。",
        "status": "active",
        "sort_order": 30,
    },
    {
        "key": "architecture",
        "label": "構成",
        "description": "構成図、接続関係、環境一覧などがある案件で使う。",
        "status": "suggested",
        "sort_order": 40,
    },
    {
        "key": "detail_design",
        "label": "詳細設計",
        "description": "パラメーターシート、設定値、設計書を扱う案件で使う。",
        "status": "suggested",
        "sort_order": 50,
    },
    {
        "key": "verification",
        "label": "検証",
        "description": "テスト計画、検証項目、結果報告を扱う案件で使う。",
        "status": "suggested",
        "sort_order": 60,
    },
]


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _nullable_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _one_of(value: Any, allowed: set[str], fallback: str) -> str:
    text = _clean_text(value, fallback)
    return text if text in allowed else fallback


def _project_info_category_key(label: str) -> str:
    import re

    key = re.sub(r"[^a-zA-Z0-9_\-\u3040-\u30ff\u3400-\u9fff]+", "_", label)
    key = key.strip("_").lower()
    return key[:120] or f"category_{datetime.utcnow().timestamp():.0f}"


def _parse_optional_uuid(value: str) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    return UUID(text)


def _clamp_project_info_importance(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(10, parsed))


def _parse_json_array(payload: str, field_name: str) -> list[Any]:
    text = (payload or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{field_name} must be a JSON array.")
    return data


def _normalize_record_field_type(value: Any) -> str:
    field_type = str(value or "text").strip()
    return field_type if field_type in record_field_types else "text"


def _record_field_key(label: str) -> str:
    import re

    key = re.sub(r"[^a-zA-Z0-9_\-\u3040-\u30ff\u3400-\u9fff]+", "_", label)
    key = key.strip("_").lower()
    return key or f"field_{datetime.utcnow().timestamp():.0f}"


async def _unique_record_field_key(
    session,
    table_id: UUID,
    label: str,
    reserved_keys: set[str] | None = None,
) -> str:
    from ...memory.models import RecordField

    base = _record_field_key(label)
    result = await session.execute(
        select(RecordField.key).where(
            RecordField.table_id == table_id,
            RecordField.deleted_at.is_(None),
        )
    )
    taken = set(result.scalars().all())
    if reserved_keys:
        taken.update(reserved_keys)
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def _normalize_record_columns(
    columns_payload: str,
    rows: list[Any],
) -> list[dict[str, str]]:
    columns = _parse_json_array(columns_payload, "columns_json")
    normalized: list[dict[str, str]] = []
    for item in columns:
        if isinstance(item, str):
            label = item.strip()
            field_type = "text"
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or item.get("key") or "").strip()
            field_type = _normalize_record_field_type(
                item.get("field_type") or item.get("type") or item.get("fieldType")
            )
        else:
            continue
        if label:
            normalized.append({"label": label, "field_type": field_type})

    if normalized:
        return normalized

    inferred: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            label = str(key).strip()
            if label and label not in inferred:
                inferred.append(label)
    return [
        {"label": label, "field_type": "text"}
        for label in (inferred or ["Title"])
    ]


def _materialize_record_row(values: dict[str, Any], fields: list[Any]) -> dict[str, Any]:
    title_field = next((field for field in fields if field.is_title), None)
    if title_field is None and fields:
        title_field = fields[0]
    title_value = values.get(title_field.key) if title_field else None
    title = None if title_value in (None, "") else str(title_value)[:500]
    search_text = " ".join(
        str(values.get(field.key))
        for field in fields
        if values.get(field.key) not in (None, "")
    )[:8000]
    return {"title": title, "search_text": search_text}


def _row_payload_to_values(row: Any, fields: list[Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    values: dict[str, Any] = {}
    casefold_lookup = {
        str(key).casefold(): value for key, value in row.items()
    }
    for field in fields:
        value = None
        if field.key in row:
            value = row[field.key]
        elif field.label in row:
            value = row[field.label]
        else:
            value = casefold_lookup.get(str(field.key).casefold())
            if value is None:
                value = casefold_lookup.get(str(field.label).casefold())
        if value is not None:
            values[field.key] = value
    return values


wbs_record_field_defs = [
    {"key": "title", "label": "Title", "field_type": "text", "sort_order": 0, "is_title": True},
    {"key": "wbs_id", "label": "WBS ID", "field_type": "text", "sort_order": 1},
    {"key": "status", "label": "Status", "field_type": "select", "sort_order": 2},
    {"key": "priority", "label": "Priority", "field_type": "select", "sort_order": 3},
    {"key": "planned_start", "label": "Planned start", "field_type": "date", "sort_order": 4},
    {"key": "planned_end", "label": "Planned end", "field_type": "date", "sort_order": 5, "is_due": True},
    {"key": "actual_start", "label": "Actual start", "field_type": "date", "sort_order": 6},
    {"key": "actual_end", "label": "Actual end", "field_type": "date", "sort_order": 7},
    {"key": "assignee", "label": "Assignee", "field_type": "text", "sort_order": 8},
    {"key": "progress", "label": "Progress", "field_type": "number", "sort_order": 9},
    {"key": "request_text", "label": "Request", "field_type": "long_text", "sort_order": 10},
    {"key": "sheet_name", "label": "Sheet", "field_type": "text", "sort_order": 11},
    {"key": "row_number", "label": "Row", "field_type": "number", "sort_order": 12},
]


issue_record_field_defs = [
    {"key": "number", "label": "No", "field_type": "number", "sort_order": 0},
    {"key": "title", "label": "課題概要", "field_type": "text", "sort_order": 1, "is_title": True},
    {"key": "status", "label": "Status", "field_type": "select", "sort_order": 2},
    {"key": "importance", "label": "重要度", "field_type": "select", "sort_order": 3},
    {"key": "kind", "label": "区分", "field_type": "select", "sort_order": 4},
    {"key": "phase", "label": "フェーズ", "field_type": "text", "sort_order": 5},
    {"key": "due_date", "label": "対応期限", "field_type": "date", "sort_order": 6, "is_due": True},
    {"key": "owner", "label": "主担当者", "field_type": "text", "sort_order": 7},
    {"key": "detail", "label": "課題詳細", "field_type": "long_text", "sort_order": 8},
    {"key": "action_plan", "label": "ActionPlan", "field_type": "long_text", "sort_order": 9},
    {"key": "close_condition", "label": "課題Close条件", "field_type": "long_text", "sort_order": 10},
    {"key": "history", "label": "対応経緯", "field_type": "long_text", "sort_order": 11},
    {"key": "created_at", "label": "起票日", "field_type": "date", "sort_order": 12},
    {"key": "reporter", "label": "起票者", "field_type": "text", "sort_order": 13},
    {"key": "resolved_at", "label": "対策終了日", "field_type": "date", "sort_order": 14},
    {"key": "approved_at", "label": "完了承認日", "field_type": "date", "sort_order": 15},
    {"key": "approver", "label": "完了承認者", "field_type": "text", "sort_order": 16},
    {"key": "notes", "label": "備考", "field_type": "long_text", "sort_order": 17},
    {"key": "source_file", "label": "Source file", "field_type": "file", "sort_order": 18},
    {"key": "source_ref", "label": "Source ref", "field_type": "text", "sort_order": 19},
]


def _wbs_record_values(row: Any) -> dict[str, Any]:
    return {
        "title": row.title,
        "wbs_id": row.wbs_id,
        "status": row.status,
        "priority": row.priority,
        "planned_start": row.planned_start,
        "planned_end": row.planned_end,
        "actual_start": row.actual_start,
        "actual_end": row.actual_end,
        "assignee": row.assignee,
        "progress": None if row.progress is None else round(row.progress * 100),
        "request_text": row.request_text,
        "sheet_name": row.sheet_name,
        "row_number": row.row_number,
    }


def _issue_record_values(row: Any) -> dict[str, Any]:
    return {
        "number": row.number,
        "title": row.title,
        "status": row.status,
        "importance": row.importance,
        "kind": row.kind,
        "phase": row.phase,
        "due_date": row.due_date,
        "owner": row.owner,
        "detail": row.detail,
        "action_plan": row.action_plan,
        "close_condition": row.close_condition,
        "history": row.history,
        "created_at": row.created_at,
        "reporter": row.reporter,
        "resolved_at": row.resolved_at,
        "approved_at": row.approved_at,
        "approver": row.approver,
        "notes": row.notes,
        "source_file": row.file_path,
        "source_ref": f"{row.sheet_name}!{row.row_number}",
    }


async def _sync_wbs_record_table(
    session,
    project_id: UUID,
    user_id: UUID,
    rows: list[Any],
    dry_run: bool,
) -> dict[str, Any]:
    from ...memory.models import RecordField, RecordRow, RecordTable, RecordView

    table_result = await session.execute(
        select(RecordTable)
        .where(
            RecordTable.project_id == project_id,
            RecordTable.name == "WBS",
            RecordTable.deleted_at.is_(None),
        )
        .limit(1)
    )
    table = table_result.scalar_one_or_none()
    table_created = False
    if table is None:
        if dry_run:
            return {
                "table_created": True,
                "created": len(rows),
                "updated": 0,
                "unchanged": 0,
            }
        table = RecordTable(
            project_id=project_id,
            name="WBS",
            description="Imported from project WBS Excel.",
            sort_order=0,
            memory_policy="project_only",
            default_sensitivity="normal",
            created_by=user_id,
            table_metadata={"source": "wbs"},
        )
        session.add(table)
        await session.flush()
        table_created = True
        session.add(
            RecordView(
                table_id=table.id,
                name="Grid",
                view_type="grid",
                config={},
                sort_order=0,
                created_by=user_id,
            )
        )

    fields_result = await session.execute(
        select(RecordField)
        .where(
            RecordField.table_id == table.id,
            RecordField.deleted_at.is_(None),
        )
        .order_by(RecordField.sort_order, RecordField.created_at)
    )
    fields = list(fields_result.scalars().all())
    existing_keys = {field.key for field in fields}
    missing_fields = [
        field for field in wbs_record_field_defs if field["key"] not in existing_keys
    ]
    if missing_fields and not dry_run:
        for field_def in missing_fields:
            field = RecordField(
                table_id=table.id,
                key=field_def["key"],
                label=field_def["label"],
                field_type=field_def["field_type"],
                sort_order=field_def["sort_order"],
                is_title=bool(field_def.get("is_title")),
                is_due=bool(field_def.get("is_due")),
                options={},
            )
            session.add(field)
            fields.append(field)
        await session.flush()

    rows_result = await session.execute(
        select(RecordRow).where(
            RecordRow.table_id == table.id,
            RecordRow.deleted_at.is_(None),
        )
    )
    by_source_key: dict[str, Any] = {}
    for existing_row in rows_result.scalars().all():
        metadata = existing_row.row_metadata or {}
        source_key = (
            metadata.get("source_key")
            if isinstance(metadata.get("source_key"), str)
            else None
        )
        if source_key:
            by_source_key[source_key] = existing_row

    created = 0
    updated = 0
    unchanged = 0
    for row in rows:
        existing = by_source_key.get(row.source_key)
        metadata = {
            "source": "wbs",
            "source_key": row.source_key,
            "row_hash": row.row_hash,
            "file_path": row.file_path,
            "sheet_name": row.sheet_name,
            "row_number": row.row_number,
            "last_synced_at": datetime.utcnow().isoformat(),
        }
        if existing is None:
            created += 1
            if dry_run:
                continue
            values = _wbs_record_values(row)
            materialized = _materialize_record_row(values, fields)
            session.add(
                RecordRow(
                    table_id=table.id,
                    project_id=project_id,
                    created_by=user_id,
                    values=values,
                    title=materialized["title"],
                    due_at=_parse_datetime(f"{row.planned_end}T00:00:00")
                    if row.planned_end
                    else None,
                    search_text=materialized["search_text"],
                    sensitivity=table.default_sensitivity or "normal",
                    row_metadata=metadata,
                )
            )
            continue

        old_metadata = existing.row_metadata or {}
        if old_metadata.get("row_hash") == row.row_hash:
            unchanged += 1
            continue
        updated += 1
        if dry_run:
            continue
        values = _wbs_record_values(row)
        materialized = _materialize_record_row(values, fields)
        existing.values = values
        existing.title = materialized["title"]
        existing.due_at = (
            _parse_datetime(f"{row.planned_end}T00:00:00")
            if row.planned_end
            else None
        )
        existing.search_text = materialized["search_text"]
        existing.row_metadata = {**old_metadata, **metadata}
        existing.updated_at = datetime.utcnow()

    return {
        "table_id": str(table.id) if table.id else None,
        "table_created": table_created,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
    }


async def _sync_issue_record_table(
    session,
    project_id: UUID,
    user_id: UUID,
    rows: list[Any],
    dry_run: bool,
    source_file: str | None = None,
) -> dict[str, Any]:
    from ...memory.models import RecordField, RecordRow, RecordTable, RecordView

    table_result = await session.execute(
        select(RecordTable)
        .where(
            RecordTable.project_id == project_id,
            RecordTable.name == "課題管理表",
            RecordTable.deleted_at.is_(None),
        )
        .limit(1)
    )
    table = table_result.scalar_one_or_none()
    table_created = False
    if table is None:
        if dry_run:
            return {
                "table_created": True,
                "created": len(rows),
                "updated": 0,
                "unchanged": 0,
                "source_file": source_file,
            }
        table = RecordTable(
            project_id=project_id,
            name="課題管理表",
            description="Imported from project issue tracker Excel.",
            sort_order=1,
            memory_policy="project_only",
            default_sensitivity="normal",
            created_by=user_id,
            table_metadata={"source": "issue_excel", "source_file": source_file},
        )
        session.add(table)
        await session.flush()
        table_created = True
        session.add(
            RecordView(
                table_id=table.id,
                name="Grid",
                view_type="grid",
                config={},
                sort_order=0,
                created_by=user_id,
            )
        )

    fields_result = await session.execute(
        select(RecordField)
        .where(
            RecordField.table_id == table.id,
            RecordField.deleted_at.is_(None),
        )
        .order_by(RecordField.sort_order, RecordField.created_at)
    )
    fields = list(fields_result.scalars().all())
    existing_keys = {field.key for field in fields}
    missing_fields = [
        field for field in issue_record_field_defs if field["key"] not in existing_keys
    ]
    if missing_fields and not dry_run:
        for field_def in missing_fields:
            field = RecordField(
                table_id=table.id,
                key=field_def["key"],
                label=field_def["label"],
                field_type=field_def["field_type"],
                sort_order=field_def["sort_order"],
                is_title=bool(field_def.get("is_title")),
                is_due=bool(field_def.get("is_due")),
                options={},
            )
            session.add(field)
            fields.append(field)
        await session.flush()

    rows_result = await session.execute(
        select(RecordRow).where(
            RecordRow.table_id == table.id,
            RecordRow.deleted_at.is_(None),
        )
    )
    by_source_key: dict[str, Any] = {}
    for existing_row in rows_result.scalars().all():
        metadata = existing_row.row_metadata or {}
        row_source_key = (
            metadata.get("source_key")
            if isinstance(metadata.get("source_key"), str)
            else None
        )
        if row_source_key:
            by_source_key[row_source_key] = existing_row

    created = 0
    updated = 0
    unchanged = 0
    for row in rows:
        existing = by_source_key.get(row.source_key)
        metadata = {
            "source": "issue_excel",
            "source_key": row.source_key,
            "row_hash": row.row_hash,
            "file_path": row.file_path,
            "sheet_name": row.sheet_name,
            "row_number": row.row_number,
            "last_synced_at": datetime.utcnow().isoformat(),
        }
        if existing is None:
            created += 1
            if dry_run:
                continue
            values = _issue_record_values(row)
            materialized = _materialize_record_row(values, fields)
            session.add(
                RecordRow(
                    table_id=table.id,
                    project_id=project_id,
                    created_by=user_id,
                    values=values,
                    title=materialized["title"],
                    due_at=_parse_datetime(f"{row.due_date}T00:00:00")
                    if row.due_date
                    else None,
                    search_text=materialized["search_text"],
                    sensitivity=table.default_sensitivity or "normal",
                    row_metadata=metadata,
                )
            )
            continue

        old_metadata = existing.row_metadata or {}
        if old_metadata.get("row_hash") == row.row_hash:
            unchanged += 1
            continue
        updated += 1
        if dry_run:
            continue
        values = _issue_record_values(row)
        materialized = _materialize_record_row(values, fields)
        existing.values = values
        existing.title = materialized["title"]
        existing.due_at = (
            _parse_datetime(f"{row.due_date}T00:00:00")
            if row.due_date
            else None
        )
        existing.search_text = materialized["search_text"]
        existing.row_metadata = {**old_metadata, **metadata}
        existing.updated_at = datetime.utcnow()

    if table.table_metadata is None:
        table.table_metadata = {}
    if not dry_run:
        table.table_metadata = {
            **(table.table_metadata or {}),
            "source": "issue_excel",
            "source_file": source_file,
            "last_synced_at": datetime.utcnow().isoformat(),
        }
        table.updated_at = datetime.utcnow()

    return {
        "table_id": str(table.id) if table.id else None,
        "table_created": table_created,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "source_file": source_file,
        "missing_field_count": len(missing_fields),
    }


async def _resolve_record_table(session, project_id: UUID, table_ref: str):
    from ...memory.models import RecordTable

    target = (table_ref or "").strip()
    if not target:
        raise ValueError("record_table is required.")

    try:
        parsed_table_id = UUID(target)
    except ValueError:
        parsed_table_id = None

    if parsed_table_id is not None:
        table = await session.get(RecordTable, parsed_table_id)
        if table and table.project_id == project_id and table.deleted_at is None:
            return table

    result = await session.execute(
        select(RecordTable).where(
            RecordTable.project_id == project_id,
            RecordTable.deleted_at.is_(None),
        )
    )
    tables = result.scalars().all()
    normalized = target.casefold()
    exact = [table for table in tables if table.name.casefold() == normalized]
    if len(exact) == 1:
        return exact[0]
    partial = [table for table in tables if normalized in table.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(exact) > 1 or len(partial) > 1:
        raise ValueError(f"Record table reference is ambiguous: {table_ref}")
    raise ValueError(f"Record table not found: {table_ref}")


async def _ensure_project_info_defaults(
    session,
    project_id: UUID,
    user_id: UUID,
) -> None:
    from ...memory.models import ProjectInfoCategory

    result = await session.execute(
        select(ProjectInfoCategory.key).where(
            ProjectInfoCategory.project_id == project_id
        )
    )
    existing_keys = set(result.scalars().all())
    for item in default_project_info_categories:
        if item["key"] in existing_keys:
            continue
        session.add(
            ProjectInfoCategory(
                project_id=project_id,
                key=item["key"],
                label=item["label"],
                description=item["description"],
                status=item["status"],
                source="template",
                sort_order=item["sort_order"],
                created_by=user_id,
            )
        )
    await session.flush()


async def _resolve_project_info_category(
    session,
    project_id: UUID,
    category_ref: str = "",
    category_id: str = "",
    create_if_missing: bool = False,
    user_id: UUID | None = None,
    status: str = "suggested",
):
    from ...memory.models import ProjectInfoCategory

    target = (category_id or category_ref or "").strip()
    if not target:
        return None

    try:
        parsed_category_id = UUID(target)
    except ValueError:
        parsed_category_id = None

    if parsed_category_id is not None:
        category = await session.get(ProjectInfoCategory, parsed_category_id)
        if category and category.project_id == project_id:
            return category

    result = await session.execute(
        select(ProjectInfoCategory).where(
            ProjectInfoCategory.project_id == project_id,
            ProjectInfoCategory.status != "archived",
        )
    )
    categories = list(result.scalars().all())
    normalized = target.casefold()
    exact = [
        category
        for category in categories
        if normalized
        in {
            str(category.id).casefold(),
            category.key.casefold(),
            category.label.casefold(),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        category
        for category in categories
        if normalized
        and (
            normalized in category.key.casefold()
            or normalized in category.label.casefold()
        )
    ]
    if len(partial) == 1:
        return partial[0]
    if len(exact) > 1 or len(partial) > 1:
        raise ValueError(f"Project information category is ambiguous: {target}")

    if not create_if_missing:
        return None

    existing_keys = {category.key for category in categories}
    base_key = _project_info_category_key(target)
    key = base_key
    index = 2
    while key in existing_keys:
        key = f"{base_key[:114]}_{index}"[:120]
        index += 1

    max_sort = await session.execute(
        select(func.max(ProjectInfoCategory.sort_order)).where(
            ProjectInfoCategory.project_id == project_id
        )
    )
    category = ProjectInfoCategory(
        project_id=project_id,
        key=key,
        label=target[:200],
        description=None,
        status=_one_of(status, project_info_category_statuses, "suggested"),
        source="agent",
        sort_order=float(max_sort.scalar_one_or_none() or 0) + 10,
        created_by=user_id,
    )
    session.add(category)
    await session.flush()
    return category


def _management_documents_from_project(project) -> list[dict[str, Any]]:
    from ...services.project_context import normalize_project_metadata

    metadata = normalize_project_metadata(project.project_metadata)
    management = metadata.get("management", {})
    docs: list[dict[str, Any]] = []

    def _append(kind: str, title: str, file_path: str | None) -> None:
        if not file_path:
            return
        docs.append(
            {
                "id": f"management:{kind}:{file_path}",
                "title": title,
                "document_type": kind,
                "target_kind": "file",
                "file_path": file_path,
                "role": "management",
                "status": "active",
                "source_type": "project_management",
                "synthetic": True,
            }
        )

    _append("wbs", "WBS", management.get("wbs_file"))
    _append("issue", "課題管理表", management.get("issue_file"))
    _append("risk", "リスク管理表", management.get("risk_file"))
    for file_path in management.get("request_files", []):
        docs.append(
            {
                "id": f"management:request:{file_path}",
                "title": str(file_path).split("/")[-1] or "補助資料",
                "document_type": "support",
                "target_kind": "file",
                "file_path": file_path,
                "role": "reference",
                "status": "active",
                "source_type": "project_management",
                "synthetic": True,
            }
        )
    return docs
