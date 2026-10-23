"""Bounded Task read projections for MCP and low-bandwidth clients."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.orm import load_only, selectinload

from ....memory.models import (
    Project,
    Tag,
    Task,
    TaskAssignee,
    TaskActivity,
    TaskComment,
    TaskOccurrence,
    TaskReference,
    TaskRelation,
    TaskTag,
    TimeEntry,
    User,
)
from ....services.task_management._shared import TaskManagementError
from ...uuid_http import parse_uuid_or_400
from ._shared import _parse_browse_scope


_DETAIL_SECTIONS = {
    "task",
    "description",
    "metadata",
    "comments",
    "references",
    "activities",
    "occurrences",
    "time_entries",
}


def _query_int(
    request: Request,
    name: str,
    default: int,
    minimum: int,
    maximum: int | None,
) -> int:
    raw_values = request.query_params.getlist(name)
    if not raw_values:
        return default
    if len(raw_values) != 1:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")
    try:
        value = int(raw_values[0])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {name}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _task_excerpt(value: str | None, maximum: int) -> str:
    normalized = " ".join((value or "").split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: max(0, maximum - 1)].rstrip() + "…"


def _text_page(
    *,
    section: str,
    task_id: UUID,
    text_value: str,
    offset: int,
    max_chars: int,
    requested_fingerprint: str | None,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
    if offset > 0 and not requested_fingerprint:
        raise HTTPException(
            status_code=400,
            detail={"code": "fingerprint_required", "detail": "Send the returned fingerprint to continue"},
        )
    if requested_fingerprint and requested_fingerprint != fingerprint:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_read_stale", "detail": "Task text changed between pages"},
        )
    end = min(len(text_value), offset + max_chars)
    return {
        "section": section,
        "task_id": str(task_id),
        "content": text_value[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text_value) else None,
        "has_more": end < len(text_value),
        "fingerprint": fingerprint,
    }


async def get_task_section(
    session: Any,
    *,
    service: Any,
    user_id: UUID,
    task_id: UUID,
    request: Request,
    serialize_reference: Callable[..., Any],
    serialize_relation: Callable[..., Any],
) -> dict[str, Any]:
    section = request.query_params.get("section")
    if section not in _DETAIL_SECTIONS:
        raise HTTPException(status_code=400, detail="Invalid section")
    limit = _query_int(request, "limit", 50, 1, 100)
    offset = _query_int(
        request,
        "offset",
        0,
        0,
        None if section in {"description", "metadata"} else 10000,
    )
    max_chars = _query_int(request, "max_chars", 4000, 1, 20000)
    fingerprint = request.query_params.get("fingerprint")

    browse_project_id, browse_space_id = _parse_browse_scope(request)
    result = await session.execute(
        select(Task.project_id).where(Task.id == task_id, Task.deleted_at.is_(None))
    )
    project_id = result.scalar_one_or_none()
    if project_id is None:
        raise TaskManagementError("Task not found", status_code=404)
    browse_project_ids: set[UUID] | None = None
    if browse_project_id is not None or browse_space_id is not None:
        browse_project_ids = set(
            await service.resolve_browse_project_ids(
                session,
                user_id=user_id,
                browse_project_id=browse_project_id,
                browse_space_id=browse_space_id,
            )
        )
        if project_id not in browse_project_ids:
            raise TaskManagementError("Task not found", status_code=404)
    await service.require_project_permission(
        session,
        project_id=project_id,
        user_id=user_id,
        permission="read",
    )

    if section == "task":
        task_row = await session.execute(
            select(Task, Project.name, func.substr(Task.description, 1, 241))
            .options(
                load_only(
                    Task.id,
                    Task.project_id,
                    Task.knowledge_node_id,
                    Task.title,
                    Task.status,
                    Task.priority,
                    Task.start_at,
                    Task.end_at,
                    Task.all_day,
                    Task.auto_close_on_due,
                    Task.reminder_offsets,
                    Task.notifications_enabled,
                    Task.source,
                    Task.created_by,
                    Task.completed_at,
                    Task.archived_at,
                    Task.created_at,
                    Task.updated_at,
                    Task.deleted_at,
                    Task.deletion_batch_id,
                    Task.estimated_hours,
                    Task.sort_order,
                    Task.parent_task_id,
                ),
                selectinload(Task.recurrence_rule),
            )
            .join(Project, Project.id == Task.project_id)
            .where(Task.id == task_id, Task.deleted_at.is_(None))
        )
        row = task_row.one_or_none()
        if row is None:
            raise TaskManagementError("Task not found", status_code=404)
        task, project_name, description_excerpt = row
        assignee_rows = (
            await session.execute(
                select(TaskAssignee, User.username, User.display_name)
                .join(User, User.id == TaskAssignee.user_id)
                .where(TaskAssignee.task_id == task_id)
                .order_by(
                    TaskAssignee.is_primary.desc(),
                    TaskAssignee.assigned_at.asc(),
                    TaskAssignee.user_id.asc(),
                )
                .limit(10)
            )
        ).all()
        tag_rows = (
            await session.execute(
                select(TaskTag.tag_id, Tag.name, Tag.color)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .where(TaskTag.task_id == task_id)
                .order_by(Tag.name.asc(), Tag.id.asc())
                .limit(10)
            )
        ).all()
        return {
            "section": "task",
            "task": {
                "id": str(task.id),
                "title": task.title,
                "status": task.status,
                "priority": task.priority,
                "project_id": str(task.project_id),
                "knowledge_node_id": (
                    str(task.knowledge_node_id) if task.knowledge_node_id else None
                ),
                "project_name": project_name,
                "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
                "start_at": _iso(task.start_at),
                "end_at": _iso(task.end_at),
                "all_day": bool(task.all_day),
                "auto_close_on_due": bool(task.auto_close_on_due),
                "reminder_offsets": list(task.reminder_offsets or []),
                "notifications_enabled": bool(task.notifications_enabled),
                "source": task.source,
                "created_by": str(task.created_by) if task.created_by else None,
                "completed_at": _iso(task.completed_at),
                "archived_at": _iso(task.archived_at),
                "created_at": _iso(task.created_at),
                "updated_at": _iso(task.updated_at),
                "deletion_batch_id": (
                    str(task.deletion_batch_id) if task.deletion_batch_id else None
                ),
                "estimated_hours": task.estimated_hours,
                "sort_order": task.sort_order,
                "recurrence_rule": (
                    task.recurrence_rule.to_dict() if task.recurrence_rule else None
                ),
                "assignees": [
                    {
                        "id": str(assignee.user_id),
                        "display_name": display_name,
                        "username": username,
                    }
                    for assignee, username, display_name in assignee_rows
                ],
                "tags": [
                    {
                        "id": str(tag_id),
                        "name": name,
                        "color": color,
                    }
                    for tag_id, name, color in tag_rows
                ],
                "description_excerpt": _task_excerpt(description_excerpt, 240),
            },
        }

    if section in {"description", "metadata"}:
        selected = (
            select(Task.description, Task.task_metadata)
            .where(Task.id == task_id, Task.deleted_at.is_(None))
        )
        row = (await session.execute(selected)).one_or_none()
        if row is None:
            raise TaskManagementError("Task not found", status_code=404)
        if section == "description":
            text_value = str(row.description or "")
        else:
            text_value = json.dumps(
                row.task_metadata or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return _text_page(
            section=section,
            task_id=task_id,
            text_value=text_value,
            offset=offset,
            max_chars=max_chars,
            requested_fingerprint=fingerprint,
        )

    write_access = browse_project_ids is None
    if write_access:
        try:
            await service.require_project_permission(
                session,
                project_id=project_id,
                user_id=user_id,
                permission="write",
            )
        except TaskManagementError:
            write_access = False

    if section == "comments":
        rows = (
            await session.execute(
                select(TaskComment, User.username, User.display_name)
                .outerjoin(User, TaskComment.user_id == User.id)
                .where(TaskComment.task_id == task_id)
                .order_by(TaskComment.created_at.desc(), TaskComment.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        ).all()
        has_more = len(rows) > limit
        page = rows[:limit]
        comments = []
        for comment, username, display_name in page:
            content = comment.content or ""
            truncated = len(content) > max_chars
            comments.append(
                {
                    "id": str(comment.id),
                    "task_id": str(comment.task_id),
                    "user_id": str(comment.user_id),
                    "content": content[:max_chars],
                    "content_truncated": truncated,
                    "content_fingerprint": (
                        hashlib.sha256(content.encode("utf-8")).hexdigest()
                        if truncated
                        else None
                    ),
                    "created_at": _iso(comment.created_at),
                    "updated_at": _iso(comment.updated_at),
                    "username": username,
                    "display_name": display_name,
                }
            )
        return {"section": section, "items": comments, "offset": offset, "has_more": has_more}

    if section == "activities":
        rows = (
            await session.execute(
                select(TaskActivity, User.username, User.display_name)
                .outerjoin(User, TaskActivity.user_id == User.id)
                .where(TaskActivity.task_id == task_id)
                .order_by(TaskActivity.created_at.desc(), TaskActivity.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        ).all()
        has_more = len(rows) > limit
        items = []
        for activity, username, display_name in rows[:limit]:
            payload_text = json.dumps(
                activity.payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            truncated = len(payload_text) > max_chars
            items.append(
                {
                    "id": str(activity.id),
                    "task_id": str(activity.task_id),
                    "user_id": str(activity.user_id) if activity.user_id else None,
                    "activity_type": activity.activity_type,
                    "payload_excerpt": payload_text[:max_chars],
                    "payload_truncated": truncated,
                    "payload_fingerprint": (
                        hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                        if truncated
                        else None
                    ),
                    "created_at": _iso(activity.created_at),
                    "username": username,
                    "display_name": display_name,
                }
            )
        return {"section": section, "items": items, "offset": offset, "has_more": has_more}

    if section == "occurrences":
        rows = (
            await session.execute(
                select(TaskOccurrence)
                .where(TaskOccurrence.task_id == task_id, TaskOccurrence.deleted_at.is_(None))
                .order_by(TaskOccurrence.start_at.asc(), TaskOccurrence.id.asc())
                .limit(limit + 1)
                .offset(offset)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        items = [
            {
                "id": str(item.id),
                "task_id": str(item.task_id),
                "start_at": _iso(item.start_at),
                "end_at": _iso(item.end_at),
                "original_start_at": _iso(item.canonical_start_at()),
                "status": item.status,
                "all_day": bool(item.all_day),
                "reminder_offsets": list(item.reminder_offsets or []),
                "source_kind": item.source_kind,
                "is_generated": bool(item.is_generated),
                "updated_at": _iso(item.updated_at),
            }
            for item in rows[:limit]
        ]
        return {"section": section, "items": items, "offset": offset, "has_more": has_more}

    if section == "time_entries":
        rows = (
            await session.execute(
                select(TimeEntry)
                .options(
                    selectinload(TimeEntry.task)
                    .selectinload(Task.project)
                    .selectinload(Project.space)
                )
                .where(TimeEntry.task_id == task_id, TimeEntry.deleted_at.is_(None))
                .order_by(TimeEntry.started_at.desc(), TimeEntry.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        items = [service._build_time_entry_payload(item) for item in rows[:limit]]
        for item in items:
            if isinstance(item.get("note"), str) and len(item["note"]) > max_chars:
                item["note"] = item["note"][:max_chars]
                item["note_truncated"] = True
        return {"section": section, "items": items, "offset": offset, "has_more": has_more}

    # References consist of both explicit reference rows and task relations.
    # Fetch only enough rows from each ordered source to form the requested
    # merged page, then run the existing ACL-aware serializers on that page.
    reference_keys = select(
        TaskReference.created_at.label("created_at"),
        literal("reference").label("kind"),
        TaskReference.id.label("row_id"),
    ).where(TaskReference.task_id == task_id)
    relation_keys = select(
        TaskRelation.created_at.label("created_at"),
        literal("relation").label("kind"),
        TaskRelation.id.label("row_id"),
    ).where(or_(TaskRelation.task_a_id == task_id, TaskRelation.task_b_id == task_id))
    merged_keys = union_all(reference_keys, relation_keys).subquery()
    page_keys = (
        await session.execute(
            select(merged_keys.c.created_at, merged_keys.c.kind, merged_keys.c.row_id)
            .order_by(
                merged_keys.c.created_at.desc(),
                merged_keys.c.row_id.desc(),
                merged_keys.c.kind.desc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    has_more = len(page_keys) > limit
    page_keys = page_keys[:limit]
    reference_ids = [row_id for _, kind, row_id in page_keys if kind == "reference"]
    relation_ids = [row_id for _, kind, row_id in page_keys if kind == "relation"]
    reference_rows = (
        (
            await session.execute(
                select(TaskReference).where(TaskReference.id.in_(reference_ids))
            )
        ).scalars().all()
        if reference_ids
        else []
    )
    relation_rows = (
        (
            await session.execute(
                select(TaskRelation).where(TaskRelation.id.in_(relation_ids))
            )
        ).scalars().all()
        if relation_ids
        else []
    )
    references_by_id = {row.id: row for row in reference_rows}
    relations_by_id = {row.id: row for row in relation_rows}
    items = []
    for _created_at, kind, row_id in page_keys:
        if kind == "reference":
            row = references_by_id[row_id]
            serialized = await serialize_reference(
                    session,
                    row,
                    user_id=user_id,
                    can_remove=write_access,
                    allowed_project_ids=browse_project_ids,
                )
            metadata = serialized.get("metadata")
            metadata_text = json.dumps(
                metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len(metadata_text) > max_chars:
                serialized["metadata"] = metadata_text[:max_chars]
                serialized["metadata_truncated"] = True
                serialized["metadata_fingerprint"] = hashlib.sha256(
                    metadata_text.encode("utf-8")
                ).hexdigest()
            items.append(serialized)
        else:
            row = relations_by_id[row_id]
            items.append(
                await serialize_relation(
                    session,
                    row,
                    current_task_id=task_id,
                    user_id=user_id,
                    can_remove=write_access,
                    service=service,
                    allowed_project_ids=browse_project_ids,
                )
            )
    return {"section": section, "items": items, "offset": offset, "has_more": has_more}
