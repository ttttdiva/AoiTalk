"""Mobile sync API routes.

Current write support covers `tasks` and `time_entries`. Remaining tables are
pull-only local caches on mobile.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    ContentDeletionEvent,
    KnowledgeNodeShare,
    Project,
    RecordField,
    RecordRow,
    RecordTable,
    Task,
    TaskAssignee,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    KnowledgeNode,
    DocsLibrary,
    public_session_context,
)
from ..memory.project_repository import ProjectRepository
from ..services.docs_graph_service import DocsGraphService
from ..services.docs_acl import (
    accessible_project_ids as docs_accessible_project_ids,
    docs_readable_node_predicate,
    can_read_node,
    library_can_read,
    library_can_write,
)
from ..services.task_management_service import TaskManagementError, TaskManagementService
from ..services.docs_library_compat import read_docs_library_id
from ..services.docs_library_compat import with_legacy_docs_library_aliases
from ..services.docs_workspace import get_project_docs_library
from .docs_sync import (
    DOCS_PULL_LIMITS,
    DOCS_SYNC_TABLES,
    DocsOperationError,
    apply_docs_operation,
    docs_scope_digest as calculate_docs_scope_digest,
    docs_scope_revision as calculate_docs_scope_revision,
    load_current_docs_entity,
    normalize_docs_body_json,
    pull_cursor_envelope,
    pull_docs_table,
)
from .uuid_http import parse_uuid_or_400
from .story_legacy_compat import LEGACY_STORY_PULL_LIMITS, LEGACY_STORY_TABLES, pull_story_table

logger = logging.getLogger(__name__)

SYNC_TABLES = {
    "projects",
    "tasks",
    "task_occurrences",
    "time_entries",
    "conversation_sessions",
    "conversation_messages",
    "record_tables",
    "record_fields",
    "record_rows",
    *LEGACY_STORY_TABLES,
    *DOCS_SYNC_TABLES,
}

# push が Docs ハンドラ（apply_docs_operation）へ委譲するテーブル。
DOCS_PUSH_TABLES = {
    "knowledge_nodes",
    "knowledge_supertags",
    "knowledge_node_supertags",
    "knowledge_field_values",
}

SYNC_PULL_LIMITS = {
    "tasks": 1000,
    "task_occurrences": 1000,
    "time_entries": 1000,
    "conversation_sessions": 5000,
    "conversation_messages": 5000,
    "record_tables": 1000,
    "record_fields": 5000,
    "record_rows": 5000,
    **LEGACY_STORY_PULL_LIMITS,
    **DOCS_PULL_LIMITS,
}


class SyncOperation(BaseModel):
    op_id: str
    table: str
    action: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    base_updated_at: Optional[str] = None


class SyncPushPayload(BaseModel):
    operations: list[SyncOperation] = Field(default_factory=list)


class SyncPullPayload(BaseModel):
    """pull 要求の任意ボディ。

    ``docs_digests`` は「テーブル名 → 前回サーバが返したダイジェスト文字列」のエコー。
    Docs の削除伝播テーブルで、サーバの現在ダイジェストと一致すれば権威セット全量
    の返却を省く（定常時の応答をほぼ空にする）。未指定・未知テーブルは無視する。
    """

    docs_digests: dict[str, str] = Field(default_factory=dict)
    docs_cursors: dict[str, str] = Field(default_factory=dict)
    # Optional Docs composite discriminator.  ``project_id`` is meaningful
    # together with ``docs_scope_id`` on the route; non-Docs pull tables keep
    # their historical all-accessible-project behavior.
    project_id: Optional[str] = None
    docs_scope_id: Optional[str] = None
    # Additive Docs pagination metadata.  These are optional so non-Docs sync
    # and legacy v2 mobile clients retain their existing request contract.
    docs_snapshot_token: Optional[str] = None
    docs_scope_revision: Optional[str] = None


async def _read_pull_docs_digests(request: Request) -> dict[str, str]:
    """GET /pull の docs_digests を取り出す（無ければ空 dict）。

    モバイルの fetch は GET にボディを付けられないため、クエリパラメータ
    ``docs_digests``（URLエンコードした JSON object）を優先して読む。
    後方の任意ボディ読み取りはテスト・ツール向けのフォールバック。
    """
    from_query = request.query_params.get("docs_digests")
    if from_query:
        try:
            return SyncPullPayload.model_validate({"docs_digests": json.loads(from_query)}).docs_digests
        except Exception:  # noqa: BLE001
            return {}
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        return SyncPullPayload.model_validate_json(raw).docs_digests
    except Exception:  # noqa: BLE001
        # 不正なボディでも pull 全体は落とさず、従来どおり全量 pull にフォールバックする。
        return {}


async def _read_pull_docs_cursors(request: Request) -> dict[str, str]:
    """Docsテーブルごとの不透明な次ページcursorを取り出す。"""
    from_query = request.query_params.get("docs_cursors")
    if from_query:
        try:
            return SyncPullPayload.model_validate(
                {"docs_cursors": json.loads(from_query)}
            ).docs_cursors
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _read_pull_docs_snapshot_token(request: Request) -> Optional[str]:
    """Read the opaque Docs snapshot token from query or optional GET body."""

    from_query = request.query_params.get("docs_snapshot_token")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_snapshot_token
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_docs_scope_revision(request: Request) -> Optional[str]:
    """Read the opaque ACL revision from query or optional GET body."""

    from_query = request.query_params.get("docs_scope_revision")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_scope_revision
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_project_id(request: Request) -> Optional[str]:
    """Read an optional Docs project discriminator from query or GET body."""

    from_query = request.query_params.get("project_id")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).project_id
    except Exception:  # noqa: BLE001
        return None


async def _read_pull_docs_scope_id(request: Request) -> Optional[str]:
    """Read the Docs library scope from query or optional GET body."""

    from_query = request.query_params.get("docs_scope_id")
    if from_query:
        return from_query
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return SyncPullPayload.model_validate_json(raw).docs_scope_id
    except Exception:  # noqa: BLE001
        return None


def _validate_docs_opaque_token(value: Optional[str], field_name: str) -> Optional[str]:
    """Validate token shape without interpreting its opaque contents."""

    if value in (None, ""):
        return None
    # Cursor envelopes are untrusted JSON.  Do not coerce numbers/arrays into
    # strings: the Director contract requires a non-empty opaque *string* and
    # a coerced value could accidentally make malformed metadata look valid.
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    token = value.strip()
    # URL-safe random tokens are intentionally opaque.  Length limits prevent
    # an accidental unbounded query value while accepting future token formats.
    if not token or len(token) > 512 or any(ord(char) < 0x20 for char in token):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return token


def _parse_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_wall_clock_datetime(
    value: Optional[str], field_name: str
) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1]
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _ensure_not_stale(
    current_updated_at: Optional[datetime], base_updated_at: Optional[str]
) -> None:
    if current_updated_at is None or base_updated_at in (None, ""):
        return
    base_dt = _parse_datetime(base_updated_at, "base_updated_at")
    if base_dt is not None and current_updated_at > base_dt:
        raise TaskManagementError("Entity was updated on the server", status_code=409)


async def _accessible_project_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    # Use the shared Project ACL policy rather than raw membership rows.  This
    # includes project owners and global admins (who need not have a
    # ProjectMember row), while excluding disabled/read=false memberships.
    return await docs_accessible_project_ids(session, user_id)


async def _docs_sync_scopes(
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    personal_library: DocsLibrary | None = None,
    personal_workspace: DocsLibrary | None = None,
) -> list[dict[str, Any]]:
    """Return the Docs workspaces visible to mobile sync.

    The legacy endpoint only pulled the actor's personal library.  Docs ACL
    now permits explicit Personal subtree shares and canonical Project
    workspaces, so advertise every currently readable scope and let the client
    pull each scope independently (with a scope-specific cursor/digest).
    """

    # ``personal_workspace`` is accepted only as an in-process compatibility
    # alias; the canonical service/API name is ``personal_library``.
    personal_library = personal_library or personal_workspace
    if personal_library is None:
        raise ValueError("personal_library is required")
    libraries: dict[UUID, DocsLibrary] = {
        personal_library.id: personal_library,
    }
    project_scopes: list[tuple[UUID, DocsLibrary, str]] = []
    if project_ids:
        # Project Docs now live in the owner's Personal Library.  A Project
        # member must still receive a project-scoped entry even though
        # ``library_can_read`` is (correctly) owner-only for Personal
        # libraries.  Resolve through the SELECT-only pointer validator so a
        # stale/malformed project pointer is never advertised.
        for project_id in sorted(set(project_ids), key=str):
            try:
                library = await get_project_docs_library(
                    session,
                    project_id=project_id,
                    actor_user_id=user_id,
                )
            except (PermissionError, ValueError):
                library = None
            if library is None:
                continue
            can_write = await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=user_id,
                permission="write",
            )
            libraries[library.id] = library
            project_scopes.append(
                (project_id, library, "write" if can_write else "read")
            )

    shared_rows = await session.execute(
        select(DocsLibrary, KnowledgeNodeShare.permission)
        .join(KnowledgeNode, KnowledgeNode.docs_library_id == DocsLibrary.id)
        .join(KnowledgeNodeShare, KnowledgeNodeShare.node_id == KnowledgeNode.id)
        .where(
            KnowledgeNodeShare.user_id == user_id,
            DocsLibrary.library_type == "personal",
            DocsLibrary.id != personal_library.id,
            KnowledgeNode.archived_at.is_(None),
        )
    )
    shared_permissions: dict[UUID, str] = {}
    for library, permission in shared_rows.all():
        libraries[library.id] = library
        value = str(permission or "read").lower()
        if shared_permissions.get(library.id) != "write":
            shared_permissions[library.id] = "write" if value == "write" else "read"

    scopes: list[dict[str, Any]] = []
    scopes.append(
        with_legacy_docs_library_aliases(
            {
                "docs_library_id": str(personal_library.id),
                "source": "personal",
                "access": "owner",
                "read_only": False,
            },
            personal_library.id,
        )
    )
    # Keep one explicit scope per readable Project.  Multiple projects may
    # share the same owner's Personal Library; the project_id discriminator
    # prevents clients from treating the whole Personal Library as shared.
    for project_id, library, access in sorted(project_scopes, key=lambda item: str(item[0])):
        scopes.append(
            with_legacy_docs_library_aliases(
                {
                    "docs_library_id": str(library.id),
                    "project_id": str(project_id),
                    "source": "project",
                    "access": access,
                    "read_only": access == "read",
                },
                library.id,
            )
        )

    project_library_ids = {library.id for _, library, _ in project_scopes}
    for library in sorted(libraries.values(), key=lambda item: str(item.id)):
        if library.id == personal_library.id or library.id in project_library_ids:
            continue
        # Personal subtree shares are advertised only as a shared scope.  The
        # pull endpoint still applies node-level ACL filtering, so metadata
        # from unrelated private nodes cannot leak through this announcement.
        scopes.append(
            with_legacy_docs_library_aliases(
                {
                    "docs_library_id": str(library.id),
                    "source": "shared",
                    "access": shared_permissions.get(library.id, "read"),
                    "read_only": shared_permissions.get(library.id, "read") == "read",
                },
                library.id,
            )
        )
    return scopes


def _split_changes(rows: list[Any]) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        payload = row.to_dict()
        deleted_at = getattr(row, "deleted_at", None)
        if deleted_at is None:
            changes.append(payload)
        else:
            tombstone = {"id": str(row.id), "deleted_at": _iso(deleted_at)}
            deletion_batch_id = getattr(row, "deletion_batch_id", None)
            if deletion_batch_id:
                tombstone["deletion_batch_id"] = str(deletion_batch_id)
            tombstones.append(tombstone)
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _conversation_session_payload(row: ConversationSession) -> dict[str, Any]:
    payload = row.to_dict()
    payload["is_unread"] = bool(
        row.app_id is not None
        and row.development_status == "waiting_for_user"
        and row.last_activity is not None
        and (row.last_read_at is None or row.last_activity > row.last_read_at)
    )
    timestamps = [
        value
        for value in (row.last_activity, row.last_read_at, row.session_start)
        if value is not None
    ]
    payload["updated_at"] = _iso(max(timestamps) if timestamps else None)
    payload["created_at"] = _iso(row.session_start)
    payload["session_metadata"] = public_session_context(row.context) or {}
    return payload


def _record_table_payload(row: RecordTable) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "description": row.description,
        "icon": row.icon,
        "sort_order": row.sort_order,
        "schema_version": row.schema_version,
        "memory_policy": row.memory_policy,
        "default_sensitivity": row.default_sensitivity,
        "metadata": row.table_metadata or {},
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_field_payload(row: RecordField) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "key": row.key,
        "label": row.label,
        "field_type": row.field_type,
        "options": row.options or {},
        "required": bool(row.required),
        "unique_value": bool(row.unique_value),
        "sort_order": row.sort_order,
        "is_title": bool(row.is_title),
        "is_due": bool(row.is_due),
        "sensitivity": row.sensitivity,
        "metadata": row.field_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_row_payload(row: RecordRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "project_id": str(row.project_id),
        "created_by": str(row.created_by) if row.created_by else None,
        "values": row.values or {},
        "title": row.title,
        "status": row.status,
        "due_at": _iso(row.due_at),
        "search_text": row.search_text,
        "sensitivity": row.sensitivity,
        "metadata": row.row_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _split_record_changes(rows: list[Any], serializer) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        if getattr(row, "deleted_at", None) is None:
            changes.append(serializer(row))
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(row.deleted_at)})
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _conversation_user_ids(user_id: UUID) -> list[str]:
    """Return conversation owner ids visible to mobile sync.

    Existing conversation routes still store normal Web/mobile chat sessions
    under `default_user`. Mobile sync must include those legacy sessions until
    the conversation stack is fully user-scoped.
    """

    ids = [str(user_id)]
    if "default_user" not in ids:
        ids.append("default_user")
    return ids


async def _pull_projects(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = select(Project).where(Project.id.in_(project_ids))
    if since:
        stmt = stmt.where(
            or_(Project.updated_at > since, Project.deleted_at > since)
        )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().all()))


async def _pull_tasks(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(Task)
        .options(
            selectinload(Task.project),
            selectinload(Task.assignees).selectinload(TaskAssignee.user),
            selectinload(Task.recurrence_rule),
            selectinload(Task.task_tags).selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(or_(Task.updated_at > since, Task.deleted_at > since))
    stmt = stmt.order_by(Task.updated_at.desc()).limit(SYNC_PULL_LIMITS["tasks"])
    result = await session.execute(stmt)
    payload = _split_changes(list(result.scalars().unique().all()))

    # A purged task no longer has a row for the ordinary table query.  Keep
    # the append-only deletion ledger as the durable sync source so a client
    # that was offline across the physical purge still receives a tombstone
    # instead of recreating the stale task on its next push.
    ledger_stmt = select(ContentDeletionEvent).where(
        ContentDeletionEvent.entity_type == "task",
        ContentDeletionEvent.project_id.in_(project_ids),
        ContentDeletionEvent.action.in_(("deleted", "purged", "permanent_deleted")),
    )
    if since:
        ledger_stmt = ledger_stmt.where(ContentDeletionEvent.event_at > since)
    ledger_stmt = ledger_stmt.order_by(
        ContentDeletionEvent.event_at.desc()
    ).limit(SYNC_PULL_LIMITS["tasks"])
    ledger_result = await session.execute(ledger_stmt)
    latest_by_id: dict[str, ContentDeletionEvent] = {}
    for event in ledger_result.scalars().all():
        entity_id = str(event.entity_id)
        if entity_id not in latest_by_id:
            latest_by_id[entity_id] = event

    known_ids = {
        str(item.get("id"))
        for item in (*payload.get("changes", []), *payload.get("tombstones", []))
        if item.get("id") is not None
    }
    for entity_id, event in latest_by_id.items():
        if entity_id in known_ids:
            continue
        payload.setdefault("tombstones", []).append(
            {
                "id": entity_id,
                "deleted_at": _iso(event.event_at),
                "deletion_batch_id": (
                    str(event.batch_id) if event.batch_id is not None else None
                ),
            }
        )
    return payload


async def _pull_occurrences(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TaskOccurrence)
        .join(Task)
        .options(
            selectinload(TaskOccurrence.task).selectinload(Task.project),
            selectinload(TaskOccurrence.task)
            .selectinload(Task.task_tags)
            .selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(
            or_(TaskOccurrence.updated_at > since, TaskOccurrence.deleted_at > since)
        )
    stmt = stmt.order_by(TaskOccurrence.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["task_occurrences"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_time_entries(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TimeEntry)
        .join(Task)
        .options(
            selectinload(TimeEntry.task).selectinload(Task.project),
            selectinload(TimeEntry.user),
            selectinload(TimeEntry.occurrence),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(or_(TimeEntry.updated_at > since, TimeEntry.deleted_at > since))
    stmt = stmt.order_by(TimeEntry.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["time_entries"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_conversation_sessions(
    session: AsyncSession, user_id: UUID, since: Optional[datetime]
) -> dict[str, Any]:
    visible_user_ids = _conversation_user_ids(user_id)
    stmt = select(ConversationSession).where(
        ConversationSession.user_id.in_(visible_user_ids)
    )
    if since:
        stmt = stmt.where(
            or_(
                ConversationSession.last_activity > since,
                ConversationSession.last_read_at > since,
                ConversationSession.deleted_at > since,
            )
        )
    stmt = stmt.order_by(ConversationSession.last_activity.desc()).limit(
        SYNC_PULL_LIMITS["conversation_sessions"]
    )
    result = await session.execute(stmt)
    changes = []
    tombstones = []
    for row in result.scalars().all():
        if row.deleted_at is None:
            changes.append(_conversation_session_payload(row))
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(row.deleted_at)})

    active_ids_result = await session.execute(
        select(ConversationSession.id).where(
            ConversationSession.user_id.in_(visible_user_ids),
            ConversationSession.deleted_at.is_(None),
        )
    )
    authoritative_ids = [str(item) for item in active_ids_result.scalars().all()]
    return {
        "changes": changes,
        "tombstones": tombstones,
        "cursor": None,
        "authoritative_ids": authoritative_ids,
    }


async def _pull_conversation_messages(
    session: AsyncSession, user_id: UUID, since: Optional[datetime]
) -> dict[str, Any]:
    stmt = (
        select(ConversationMessage)
        .join(
            ConversationSession,
            ConversationMessage.session_id == ConversationSession.id,
        )
        .where(ConversationSession.user_id.in_(_conversation_user_ids(user_id)))
    )
    if since:
        stmt = stmt.where(
            or_(
                ConversationMessage.updated_at > since,
                ConversationMessage.deleted_at > since,
            )
        )
    stmt = stmt.order_by(ConversationMessage.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["conversation_messages"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_record_tables(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_table_payload)
    stmt = select(RecordTable).where(RecordTable.project_id.in_(project_ids))
    if since:
        stmt = stmt.where(
            or_(RecordTable.updated_at > since, RecordTable.deleted_at > since)
        )
    stmt = stmt.order_by(RecordTable.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_tables"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_table_payload)


async def _pull_record_fields(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_field_payload)
    stmt = select(RecordField).join(RecordTable).where(
        RecordTable.project_id.in_(project_ids)
    )
    if since:
        stmt = stmt.where(
            or_(RecordField.updated_at > since, RecordField.deleted_at > since)
        )
    stmt = stmt.order_by(RecordField.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_fields"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_field_payload)


async def _pull_record_rows(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_row_payload)
    stmt = select(RecordRow).where(RecordRow.project_id.in_(project_ids))
    if since:
        stmt = stmt.where(or_(RecordRow.updated_at > since, RecordRow.deleted_at > since))
    stmt = stmt.order_by(RecordRow.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_rows"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_row_payload)


async def _pull_table(
    table: str,
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    since: Optional[datetime],
    docs_docs_library_id: Optional[UUID] = None,
    docs_digests: Optional[dict[str, str]] = None,
    docs_cursors: Optional[dict[str, str]] = None,
    docs_pagination: bool = False,
    force_docs_full: bool = False,
    docs_reconcile: bool = True,
    docs_snapshot_token: Optional[str] = None,
    docs_scope_revision: Optional[str] = None,
    docs_project_id: Optional[UUID] = None,
    docs_accessible_project_ids: Optional[list[UUID]] = None,
) -> dict[str, Any]:
    if table in DOCS_SYNC_TABLES:
        if docs_docs_library_id is None:
            return {"changes": [], "tombstones": [], "cursor": None}
        return await pull_docs_table(
            table,
            session,
            docs_library_id=docs_docs_library_id,
            accessible_project_ids=(
                docs_accessible_project_ids
                if docs_accessible_project_ids is not None
                else project_ids
            ),
            scope_project_id=docs_project_id,
            since=since,
            client_digest=(docs_digests or {}).get(table),
            cursor=(docs_cursors or {}).get(table),
            paginate=docs_pagination,
            force_full=force_docs_full,
            include_authoritative_ids=docs_reconcile,
            user_id=user_id,
            snapshot_token=docs_snapshot_token,
            scope_revision=docs_scope_revision,
        )
    if table == "projects":
        return await _pull_projects(session, project_ids, since)
    if table == "tasks":
        return await _pull_tasks(session, project_ids, since)
    if table == "task_occurrences":
        return await _pull_occurrences(session, project_ids, since)
    if table == "time_entries":
        return await _pull_time_entries(session, project_ids, since)
    if table == "conversation_sessions":
        return await _pull_conversation_sessions(session, user_id, since)
    if table == "conversation_messages":
        return await _pull_conversation_messages(session, user_id, since)
    if table == "record_tables":
        return await _pull_record_tables(session, project_ids, since)
    if table == "record_fields":
        return await _pull_record_fields(session, project_ids, since)
    if table == "record_rows":
        return await _pull_record_rows(session, project_ids, since)
    if table in LEGACY_STORY_TABLES:
        return await pull_story_table(
            table,
            session,
            user_id=user_id,
            since=since,
            limit=SYNC_PULL_LIMITS[table],
        )
    return {"changes": [], "tombstones": [], "cursor": None}


def _task_updates_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "project_id" in payload:
        updates["project_id"] = parse_uuid_or_400(
            payload.get("project_id"), "project_id"
        )
    if "title" in payload:
        updates["title"] = payload.get("title")
    if "description" in payload:
        updates["description"] = payload.get("description")
    if "status" in payload:
        updates["status"] = payload.get("status")
    if "priority" in payload:
        updates["priority"] = payload.get("priority")
    if "start_at" in payload:
        updates["start_at"] = _parse_wall_clock_datetime(
            payload.get("start_at"), "start_at"
        )
    if "end_at" in payload:
        updates["end_at"] = _parse_wall_clock_datetime(
            payload.get("end_at"), "end_at"
        )
    if "all_day" in payload:
        updates["all_day"] = payload.get("all_day")
    if "auto_close_on_due" in payload:
        updates["auto_close_on_due"] = payload.get("auto_close_on_due")
    if "reminder_offsets" in payload:
        updates["reminder_offsets"] = payload.get("reminder_offsets")
    if "notifications_enabled" in payload:
        updates["notifications_enabled"] = payload.get("notifications_enabled")
    if "metadata" in payload or "task_metadata" in payload:
        updates["task_metadata"] = _task_metadata_from_payload(payload)
    if "estimated_hours" in payload:
        updates["estimated_hours"] = payload.get("estimated_hours")
    if "parent_task_id" in payload:
        updates["parent_task_id"] = (
            parse_uuid_or_400(payload.get("parent_task_id"), "parent_task_id")
            if payload.get("parent_task_id")
            else None
        )
    if "tag_ids" in payload:
        updates["tag_ids"] = [
            parse_uuid_or_400(value, "tag_id")
            for value in (payload.get("tag_ids") or [])
        ]
    if "assignee_ids" in payload:
        updates["assignee_ids"] = [
            parse_uuid_or_400(value, "assignee_id")
            for value in (payload.get("assignee_ids") or [])
        ]
    return updates


def _task_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_metadata = payload.get("metadata") or payload.get("task_metadata")
    if not isinstance(raw_metadata, dict):
        return None
    metadata = dict(raw_metadata)
    metadata.pop("mobile_sync_status", None)
    metadata.pop("mobile_sync_error", None)
    return metadata


def _time_entry_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": parse_uuid_or_400(payload.get("task_id"), "task_id"),
        "occurrence_id": parse_uuid_or_400(payload.get("occurrence_id"), "occurrence_id")
        if "occurrence_id" in payload
        else None,
        "started_at": _parse_datetime(payload.get("started_at"), "started_at")
        if "started_at" in payload
        else None,
        "ended_at": _parse_datetime(payload.get("ended_at"), "ended_at")
        if "ended_at" in payload
        else None,
        "source": str(payload.get("source") or "mobile"),
        "note": payload.get("note"),
        "entry_metadata": payload.get("metadata") or payload.get("entry_metadata") or {},
    }


def _project_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    values = {
        "name": str(payload.get("name") or "").strip(),
        "description": payload.get("description"),
        "aliases": payload.get("aliases") if isinstance(payload.get("aliases"), list) else None,
        "allow_join_requests": payload.get("allow_join_requests"),
        "storage_quota_mb": payload.get("storage_quota_mb"),
        "project_metadata": payload.get("metadata") or payload.get("project_metadata"),
    }
    if "space_id" in payload:
        values["space_id"] = (
            parse_uuid_or_400(payload.get("space_id"), "space_id")
            if payload.get("space_id")
            else None
        )
    if "is_completed" in payload:
        values["is_completed"] = bool(payload.get("is_completed"))
    return values


async def _load_current_task_payload(
    service: TaskManagementService, session: AsyncSession, task_id: UUID
) -> Optional[dict[str, Any]]:
    try:
        task = await service._load_task(session, task_id)
        return task.to_dict()
    except TaskManagementError:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            return None
        return {
            "id": str(task.id),
            "updated_at": _iso(task.updated_at),
            "deleted_at": _iso(task.deleted_at),
            "deletion_batch_id": (
                str(task.deletion_batch_id)
                if getattr(task, "deletion_batch_id", None)
                else None
            ),
        }


def _task_tombstone_payload(task: Task) -> dict[str, Any]:
    """Return the canonical sync tombstone for an already-deleted task."""

    return {
        "id": str(task.id),
        "task_id": str(task.id),
        "updated_at": _iso(task.updated_at),
        "deleted_at": _iso(task.deleted_at),
        "deletion_batch_id": (
            str(task.deletion_batch_id)
            if getattr(task, "deletion_batch_id", None)
            else None
        ),
        "idempotent": True,
    }


async def _load_current_time_entry_payload(
    service: TaskManagementService, session: AsyncSession, entry_id: UUID
) -> Optional[dict[str, Any]]:
    result = await session.execute(
        select(TimeEntry).where(TimeEntry.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return None
    if entry.deleted_at is not None:
        return {
            "id": str(entry.id),
            "updated_at": _iso(entry.updated_at),
            "deleted_at": _iso(entry.deleted_at),
            "deletion_batch_id": (
                str(entry.deletion_batch_id)
                if getattr(entry, "deletion_batch_id", None)
                else None
            ),
        }
    return await service._serialize_time_entry(session, entry.id)


async def _apply_task_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
) -> dict[str, Any]:
    task_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if task_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing_result = await session.execute(select(Task).where(Task.id == task_id))
    existing = existing_result.scalar_one_or_none()

    if operation.action == "delete":
        if existing is None:
            # A client may retry a delete after the retention worker has
            # already purged the row.  Keep the operation idempotent while
            # accurately reporting that no server tombstone remains.
            return {
                "id": str(task_id),
                "task_id": str(task_id),
                "updated_at": None,
                "deleted_at": None,
                "deletion_batch_id": None,
                "idempotent": True,
                "purged": True,
            }
        if existing.deleted_at is not None:
            # Do not re-run stale checking for a tombstone.  A repeated offline
            # delete must converge on the server's original batch/timestamp.
            return _task_tombstone_payload(existing)
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        return await service.delete_task(
            session, user_id=user_id, task_id=task_id
        )

    if operation.action == "restore":
        raw_batch_id = operation.payload.get("deletion_batch_id")
        batch_id = (
            parse_uuid_or_400(raw_batch_id, "deletion_batch_id")
            if raw_batch_id
            else None
        )
        return await service.restore_task(
            session,
            user_id=user_id,
            task_id=task_id,
            deletion_batch_id=batch_id,
        )

    if operation.action == "create":
        payload = operation.payload
        title = str(payload.get("title") or "").strip()
        if not title:
            raise TaskManagementError("title is required", status_code=400)
        if existing is not None and existing.deleted_at is not None:
            # A normal create is never an implicit restore.  The explicit
            # restore operation carries the batch guard and ACL checks.
            raise TaskManagementError(
                "Task is deleted; use restore before creating it again",
                status_code=409,
                detail={"code": "task_tombstoned"},
            )
        if existing is None:
            ledger_result = await session.execute(
                select(ContentDeletionEvent)
                .where(
                    ContentDeletionEvent.entity_type == "task",
                    ContentDeletionEvent.entity_id == str(task_id),
                    ContentDeletionEvent.action.in_(
                        ("deleted", "purged", "permanent_deleted")
                    ),
                )
                .order_by(ContentDeletionEvent.event_at.desc())
                .limit(1)
            )
            if ledger_result.scalar_one_or_none() is not None:
                raise TaskManagementError(
                    "Task was permanently removed; stale create is rejected",
                    status_code=409,
                    detail={"code": "task_purged_tombstone"},
                )
        if existing is not None and existing.deleted_at is None:
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            return await service.update_task(
                session,
                user_id=user_id,
                task_id=task_id,
                updates=_task_updates_from_payload(payload),
            )
        created = await service.create_task(
            session,
            user_id=user_id,
            task_id=task_id,
            project_id=parse_uuid_or_400(payload.get("project_id"), "project_id"),
            title=title,
            description=payload.get("description"),
            status=str(payload.get("status") or "open"),
            priority=payload.get("priority"),
            start_at=_parse_wall_clock_datetime(payload.get("start_at"), "start_at"),
            end_at=_parse_wall_clock_datetime(payload.get("end_at"), "end_at"),
            all_day=bool(payload.get("all_day") or False),
            auto_close_on_due=bool(payload.get("auto_close_on_due") or False),
            reminder_offsets=payload.get("reminder_offsets"),
            notifications_enabled=payload.get("notifications_enabled"),
            estimated_hours=payload.get("estimated_hours"),
            parent_task_id=(
                parse_uuid_or_400(payload.get("parent_task_id"), "parent_task_id")
                if payload.get("parent_task_id")
                else None
            ),
            tag_ids=[
                parse_uuid_or_400(value, "tag_id")
                for value in (payload.get("tag_ids") or [])
            ],
            assignee_ids=[
                parse_uuid_or_400(value, "assignee_id")
                for value in (payload.get("assignee_ids") or [])
            ],
            task_metadata=_task_metadata_from_payload(payload),
            source=str(payload.get("source") or "mobile"),
        )
        return created

    if operation.action == "update":
        current = await service._load_task(session, task_id)
        _ensure_not_stale(current.updated_at, operation.base_updated_at)
        return await service.update_task(
            session,
            user_id=user_id,
            task_id=task_id,
            updates=_task_updates_from_payload(operation.payload),
        )

    raise TaskManagementError("Unsupported task sync action", status_code=400)


async def _apply_time_entry_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
) -> dict[str, Any]:
    entry_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if entry_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    values = _time_entry_values_from_payload(operation.payload)
    existing_result = await session.execute(select(TimeEntry).where(TimeEntry.id == entry_id))
    existing = existing_result.scalar_one_or_none()

    if operation.action == "delete":
        if existing is None:
            return {
                "id": str(entry_id),
                "updated_at": None,
                "deleted_at": None,
                "deletion_batch_id": None,
                "idempotent": True,
                "purged": True,
            }
        if existing.deleted_at is not None:
            return {
                "id": str(existing.id),
                "updated_at": _iso(existing.updated_at),
                "deleted_at": _iso(existing.deleted_at),
                "deletion_batch_id": (
                    str(existing.deletion_batch_id)
                    if getattr(existing, "deletion_batch_id", None)
                    else None
                ),
                "idempotent": True,
            }
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await service.delete_time_entry(session, user_id=user_id, entry_id=entry_id)
        # The deleted row is intentionally hidden by the live serializer, so
        # return the exact in-memory tombstone rather than reloading it with a
        # ``deleted_at IS NULL`` predicate.
        return {
            "id": str(existing.id),
            "updated_at": _iso(existing.updated_at),
            "deleted_at": _iso(existing.deleted_at),
            "deletion_batch_id": (
                str(existing.deletion_batch_id)
                if getattr(existing, "deletion_batch_id", None)
                else None
            ),
            "idempotent": False,
        }

    if operation.action == "create":
        task_id = values["task_id"]
        started_at = values["started_at"]
        if task_id is None:
            raise TaskManagementError("task_id is required", status_code=400)
        if started_at is None:
            raise TaskManagementError("started_at is required", status_code=400)

        task = await service._load_task(session, task_id)
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if existing is not None and existing.deleted_at is None:
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            return await service._serialize_time_entry(session, existing.id)

        active_result = await session.execute(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.deleted_at.is_(None),
                TimeEntry.id != entry_id,
            )
        )
        for active_entry in active_result.scalars().all():
            active_entry.ended_at = started_at
            active_entry.updated_at = started_at

        entry = TimeEntry(
            id=entry_id,
            task_id=task.id,
            occurrence_id=values["occurrence_id"],
            user_id=user_id,
            started_at=started_at,
            ended_at=values["ended_at"],
            source=values["source"],
            note=values["note"],
            entry_metadata=values["entry_metadata"],
        )
        session.add(entry)
        if values["ended_at"] is None:
            task.status = "in_progress"
        await session.commit()
        return await service._serialize_time_entry(session, entry.id)

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Time entry not found", status_code=404)
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        return await service.update_time_entry(
            session,
            user_id=user_id,
            entry_id=entry_id,
            started_at=values["started_at"],
            ended_at=values["ended_at"] if "ended_at" in operation.payload else None,
            note=values["note"] if "note" in operation.payload else None,
        )

    raise TaskManagementError("Unsupported time entry sync action", status_code=400)


async def _apply_project_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    user_info: Optional[dict[str, Any]] = None,
    operation: SyncOperation,
    workspace_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    project_id = parse_uuid_or_400(operation.entity_id, "entity_id")
    if project_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing = await ProjectRepository.get_by_id(session, project_id)
    values = _project_values_from_payload(operation.payload)

    if operation.action == "delete":
        if existing is None:
            return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="delete"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await ProjectRepository.delete_project(
            session,
            project_id,
            delete_library=True,
            workspace_root=workspace_root,
        )
        return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}

    if operation.action == "create":
        name = values["name"]
        if not name:
            raise TaskManagementError("name is required", status_code=400)
        if existing is not None and existing.deleted_at is None:
            await service.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            if "space_id" in values and values["space_id"] is not None:
                from ..services.space_access import can_write_space

                can_write, space = await can_write_space(
                    session,
                    space_id=values["space_id"],
                    user_id=user_id,
                    user_info=user_info or {"id": str(user_id)},
                )
                if space is None:
                    raise TaskManagementError("Space not found", status_code=404)
                if not can_write:
                    raise TaskManagementError("Space access denied", status_code=403)
            updated = await ProjectRepository.update_project(
                session,
                project_id,
                **{
                    key: value
                    for key, value in values.items()
                    if value is not None or key == "space_id"
                },
            )
            return updated.to_dict() if updated else {"id": str(project_id)}
        space_id = values.get("space_id")
        if space_id is not None:
            from ..services.space_access import can_write_space

            can_write, space = await can_write_space(
                session,
                space_id=space_id,
                user_id=user_id,
                user_info=user_info or {"id": str(user_id)},
            )
            if space is None:
                raise TaskManagementError("Space not found", status_code=404)
            if not can_write:
                raise TaskManagementError("Space access denied", status_code=403)
        created = await ProjectRepository.create_project(
            session,
            owner_id=user_id,
            project_id=project_id,
            name=name,
            description=values["description"],
            aliases=values["aliases"],
            space_id=space_id,
            is_completed=bool(values.get("is_completed") or False),
            allow_join_requests=bool(
                values["allow_join_requests"]
                if values["allow_join_requests"] is not None
                else True
            ),
            storage_quota_mb=int(values["storage_quota_mb"] or 1000),
            project_metadata=values["project_metadata"] or {},
        )
        # Sync create is another Project creation path. Seed the canonical
        # information node after the repository's internal commit; failures
        # remain retryable by the Project tab's idempotent ensure endpoint.
        from ..services.project_information_docs import (
            ensure_project_information_doc,
            is_default_inbox_project,
        )
        if not is_default_inbox_project(created):
            await ensure_project_information_doc(
                session,
                project=created,
                user_id=user_id,
            )
            await session.commit()
            await session.refresh(created)
        return created.to_dict()

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Project not found", status_code=404)
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        update_values = {key: value for key, value in values.items() if value is not None}
        if "space_id" in values:
            # Explicit null moves a project back to the unclassified bucket;
            # a non-null target still has to pass the canonical Space policy.
            update_values["space_id"] = values["space_id"]
            if values["space_id"] is not None:
                from ..services.space_access import can_write_space

                can_write, space = await can_write_space(
                    session,
                    space_id=values["space_id"],
                    user_id=user_id,
                    user_info=user_info or {"id": str(user_id)},
                )
                if space is None:
                    raise TaskManagementError("Space not found", status_code=404)
                if not can_write:
                    raise TaskManagementError("Space access denied", status_code=403)
        if "name" in update_values and not update_values["name"]:
            update_values.pop("name")
        updated = await ProjectRepository.update_project(session, project_id, **update_values)
        return updated.to_dict() if updated else {"id": str(project_id)}

    raise TaskManagementError("Unsupported project sync action", status_code=400)


async def _apply_docs_push_operation(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
    workspace_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Docs push をRESTと同一の処理へ委譲し、サーバー版を検証する。"""
    if operation.table == "knowledge_nodes" and "body_json" in operation.payload and operation.payload.get("body_json") is not None:
        try:
            operation.payload["body_json"] = normalize_docs_body_json(operation.payload["body_json"])
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=400) from exc
    # app_readme の書き戻しはロックと実ファイル操作で同じ root を使う必要がある。
    # DocsGraphService に実効 root を持たせて docs_sync 側へ透過する。
    service = DocsGraphService(session, workspace_root=workspace_root)
    project_ref = operation.payload.get("project_id")
    # New clients send docs_library_id/docsLibraryId; old mobile sends
    # workspace_id/workspaceId.  Read all four explicitly at the wire boundary.
    workspace_ref = read_docs_library_id(operation.payload)
    library = None
    target_ref = (
        operation.payload.get("node_id")
        or operation.payload.get("parent_id")
        or str(operation.entity_id).split(":", 1)[0]
    )
    try:
        target_id = UUID(str(target_ref)) if target_ref else None
    except (TypeError, ValueError):
        target_id = None
    target = await session.get(KnowledgeNode, target_id) if target_id is not None else None
    if target is not None:
        # Existing node library is authoritative.  A stale project_id in an
        # update must not redirect the operation into a canonical library.
        library = await session.get(DocsLibrary, target.docs_library_id)
        target_readable = False
        if library is not None:
            target_readable = await library_can_read(session, library, user_id)
            if not target_readable:
                # Personal workspaces are shared at subtree roots, not at the
                # library row.  Check the target node ACL before returning a
                # generic not-found response for a legitimate shared edit.
                target_readable = await can_read_node(
                    session,
                    target,
                    user_id,
                    library=library,
                    include_archived=True,
                )
        if library is not None and not target_readable:
            raise DocsOperationError("Docs node not found", status_code=404)
    elif workspace_ref not in (None, ""):
        try:
            docs_library_id = UUID(str(workspace_ref))
        except (TypeError, ValueError) as exc:
            raise DocsOperationError("Invalid docs_library_id", status_code=400) from exc
        library = await session.get(DocsLibrary, docs_library_id)
        scope_readable = library is not None and await library_can_read(
            session, library, user_id
        )
        if not scope_readable and project_ref not in (None, ""):
            # A Project member's sync scope points at the owner's Personal
            # Library.  Library ownership is intentionally owner-only, so
            # validate the Project ACL/pointer as the authority instead of
            # rejecting a legitimate project create/update.
            try:
                scoped_project_id = UUID(str(project_ref))
                project_library = await service.get_project_information_library(
                    scoped_project_id,
                    user_id,
                )
            except (PermissionError, ValueError, TypeError):
                project_library = None
            scope_readable = bool(
                project_library is not None
                and library is not None
                and project_library.id == library.id
            )
        if library is None or not scope_readable:
            raise DocsOperationError("Docs library not found", status_code=404)
    elif project_ref not in (None, ""):
        try:
            project_id = UUID(str(project_ref))
        except (TypeError, ValueError) as exc:
            raise DocsOperationError("Invalid project_id", status_code=400) from exc
        try:
            library = await service.ensure_project_information_library(project_id, user_id)
        except PermissionError as exc:
            raise DocsOperationError(str(exc), status_code=403) from exc
        except ValueError as exc:
            raise DocsOperationError(str(exc), status_code=404) from exc
    if library is None:
        library = await service.ensure_library(user_id)
    return await apply_docs_operation(
        session,
        service,
        user_id=user_id,
        docs_library_id=library.id,
        table=operation.table,
        action=operation.action,
        entity_id=operation.entity_id,
        payload=operation.payload,
        base_updated_at=operation.base_updated_at,
        require_base_version=True,
    )


def create_sync_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    workspace_root: str | os.PathLike[str] | None = None,
) -> APIRouter:
    """Mobile sync router。

    ``workspace_root`` は Project 削除のロック取得先と library 削除先、および
    Docs 経由の App README 書き戻し先を一致させるために透過する。``None`` なら
    ``AOITALK_WORKSPACES_DIR`` 由来の既定 root を使う。
    """
    router = APIRouter(prefix="/api/sync", tags=["sync"])
    service = TaskManagementService()

    async def _get_current_user(request: Request) -> tuple[UUID, dict[str, Any]]:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"]), user_info

    @router.get("/pull")
    async def pull(
        request: Request,
        since: Optional[str] = None,
        tables: Optional[str] = None,
        project_id: Optional[str] = None,
        docs_scope_id: Optional[str] = None,
        docs_pagination: bool = False,
        docs_scope_digest: Optional[str] = None,
        docs_snapshot_token: Optional[str] = None,
        docs_scope_revision: Optional[str] = None,
        docs_reconcile: bool = True,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        docs_digests = await _read_pull_docs_digests(request)
        docs_cursors = await _read_pull_docs_cursors(request)
        body_snapshot_token = await _read_pull_docs_snapshot_token(request)
        body_scope_revision = await _read_pull_docs_scope_revision(request)
        body_project_id = await _read_pull_project_id(request)
        body_docs_scope_id = await _read_pull_docs_scope_id(request)
        # Query parameters win, matching the existing digest/cursor readers;
        # GET bodies remain a test/tooling fallback for composite Docs scopes.
        project_id = project_id or body_project_id
        docs_scope_id = docs_scope_id or body_docs_scope_id
        # Query parameters win, matching the existing digest/cursor readers;
        # GET bodies remain a test/tooling fallback.
        docs_snapshot_token = _validate_docs_opaque_token(
            docs_snapshot_token or body_snapshot_token,
            "docs_snapshot_token",
        )
        requested_snapshot_token = docs_snapshot_token
        cursor_snapshot_token: Optional[str] = None
        docs_scope_revision = _validate_docs_opaque_token(
            docs_scope_revision or body_scope_revision,
            "docs_scope_revision",
        )
        requested = [
            table.strip()
            for table in (tables.split(",") if tables else sorted(SYNC_TABLES))
            if table.strip()
        ]
        unsupported = [table for table in requested if table not in SYNC_TABLES]
        if unsupported:
            raise HTTPException(status_code=400, detail=f"Unsupported tables: {unsupported}")

        session = await get_db_manager().get_session()
        server_time = datetime.utcnow()
        try:
            since_dt = _parse_datetime(since, "since")
            await ProjectRepository.ensure_user_inbox_setup(session, user_id)
            await session.commit()
            project_ids = await _accessible_project_ids(session, user_id)
            # Docs テーブルが要求された場合のみ library を解決する。
            docs_docs_library_id: Optional[UUID] = None
            docs_project_id: Optional[UUID] = None
            docs_scopes: list[dict[str, Any]] = []
            docs_revision_scopes: list[dict[str, Any]] = []
            docs_acl_entries: list[tuple[Any, Any]] = []
            if any(table in DOCS_SYNC_TABLES for table in requested):
                docs_service = DocsGraphService(session)
                if docs_scope_id:
                    try:
                        library = await session.get(
                            DocsLibrary, UUID(str(docs_scope_id))
                        )
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid Docs scope") from exc
                    if library is None:
                        raise HTTPException(status_code=404, detail="Docs scope not found")
                    if project_id:
                        # A composite scope identifies one Project inside the
                        # supplied library. Resolve through the SELECT-only
                        # canonical pointer validator so a stale/malformed
                        # Project.knowledge_node_id cannot expose a sibling
                        # Project sharing the same Personal library.
                        try:
                            docs_project_id = UUID(str(project_id))
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(status_code=400, detail="Invalid project_id") from exc
                        project_row = await session.get(Project, docs_project_id)
                        if project_row is None or project_row.deleted_at is not None:
                            raise HTTPException(status_code=404, detail="Project not found")
                        try:
                            project_library = await docs_service.get_project_information_library(
                                docs_project_id,
                                user_id,
                            )
                        except PermissionError as exc:
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                ) from exc
                            raise HTTPException(status_code=403, detail=str(exc)) from exc
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(status_code=404, detail="Project not found") from exc
                        if project_library is None:
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                )
                            raise HTTPException(
                                status_code=400,
                                detail="Project Docs pointer is invalid",
                            )
                        if project_library.id != library.id:
                            raise HTTPException(
                                status_code=400,
                                detail="Project Docs pointer does not match docs_scope_id",
                            )
                    else:
                        scope_readable = await library_can_read(
                            session, library, user_id
                        )
                        if not scope_readable:
                            # Personal libraries are shared by subtree ACL;
                            # there is intentionally no library-level share.
                            scope_readable = (
                                await session.scalar(
                                    select(KnowledgeNode.id)
                                    .where(
                                        docs_readable_node_predicate(
                                            KnowledgeNode,
                                            docs_library_id=library.id,
                                            user_id=user_id,
                                            library_owner_id=getattr(library, "owner_user_id", None),
                                        )
                                    )
                                    .limit(1)
                                )
                            ) is not None
                        if not scope_readable:
                            # A scoped continuation which lost access is a
                            # resumable authorization change, not a generic 403:
                            # the mobile promotion must quarantine the revoked
                            # scope and restart from its authoritative scope set.
                            if docs_snapshot_token or docs_cursors or docs_scope_revision:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Docs sync scope revoked; restart pull",
                                )
                            raise HTTPException(status_code=403, detail="Docs scope access denied")
                elif project_id:
                    try:
                        docs_project_id = UUID(str(project_id))
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid project_id") from exc
                    project_row = await session.get(Project, docs_project_id)
                    if project_row is None or project_row.deleted_at is not None:
                        raise HTTPException(status_code=404, detail="Project not found")
                    try:
                        library = await docs_service.get_project_information_library(
                            docs_project_id, user_id
                        )
                    except PermissionError as exc:
                        if docs_snapshot_token or docs_cursors or docs_scope_revision:
                            raise HTTPException(
                                status_code=409,
                                detail="Docs sync scope revoked; restart pull",
                            ) from exc
                        raise HTTPException(status_code=403, detail=str(exc)) from exc
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=404, detail="Project not found") from exc
                    if library is None:
                        if docs_snapshot_token or docs_cursors or docs_scope_revision:
                            raise HTTPException(
                                status_code=409,
                                detail="Docs sync scope revoked; restart pull",
                            )
                        raise HTTPException(
                            status_code=400,
                            detail="Project Docs pointer is invalid",
                        )
                else:
                    library = await docs_service.ensure_library(user_id)
                docs_docs_library_id = library.id
                personal_library = (
                    library
                    if library.owner_user_id == user_id
                    else await docs_service.ensure_library(user_id)
                )
                docs_scopes = await _docs_sync_scopes(
                    session,
                    user_id=user_id,
                    project_ids=project_ids,
                    personal_library=personal_library,
                )
                # The authoritative response advertises the complete active
                # set, but each cursor/revision must hash only the selected
                # composite scope.  Otherwise Project A ACL changes would
                # invalidate an independent Project B run in the same library.
                library_scope_id = str(docs_docs_library_id)
                if docs_project_id is not None:
                    docs_revision_scopes = [
                        scope
                        for scope in docs_scopes
                        if str(
                            scope.get("docs_library_id") or scope.get("workspace_id")
                        )
                        == library_scope_id
                        and str(scope.get("project_id")) == str(docs_project_id)
                    ]
                else:
                    # The root data slice is personal-only, but its
                    # authoritative scope revision still binds the complete
                    # active discovery set. A Project revoke must invalidate
                    # an in-flight root run so promotion can quarantine it.
                    docs_revision_scopes = docs_scopes
                if docs_pagination:
                    # Include the actor's canonical node-share rows in the
                    # scope revision. Aggregate docs_scopes alone cannot
                    # distinguish a child-level downgrade/revocation inside
                    # an otherwise still-active shared library. Restrict the
                    # projection to this composite scope so Project A ACL
                    # changes do not invalidate an independent Project B run.
                    try:
                        acl_stmt = (
                            select(
                                KnowledgeNodeShare.node_id,
                                KnowledgeNodeShare.permission,
                            )
                            .join(
                                KnowledgeNode,
                                KnowledgeNode.id == KnowledgeNodeShare.node_id,
                            )
                            .where(
                                KnowledgeNodeShare.user_id == user_id,
                                KnowledgeNode.docs_library_id == docs_docs_library_id,
                            )
                        )
                        if docs_project_id is not None:
                            acl_stmt = acl_stmt.where(
                                KnowledgeNode.project_id == docs_project_id
                            )
                        acl_rows = await session.execute(acl_stmt)
                        docs_acl_entries = list(acl_rows.all())
                    except Exception:  # noqa: BLE001
                        # A rolling deployment may not have the optional
                        # share table yet. The SQL-native pull ACL remains
                        # fail-closed; omit only the additive revision input.
                        docs_acl_entries = []
                # Materialize all library metadata before commit; the async
                # session may expire ORM attributes on commit.
                await session.commit()
            docs_scope_project_ids = (
                [docs_project_id] if docs_project_id is not None else []
            )
            current_docs_scope_digest = (
                calculate_docs_scope_digest(
                    docs_docs_library_id,
                    docs_scope_project_ids,
                    scope_project_id=docs_project_id,
                )
                if docs_pagination and docs_docs_library_id is not None
                else None
            )
            current_docs_scope_revision = (
                calculate_docs_scope_revision(
                    docs_docs_library_id,
                    docs_scope_project_ids,
                    docs_revision_scopes,
                    docs_acl_entries,
                    scope_project_id=docs_project_id,
                )
                if docs_pagination and docs_docs_library_id is not None
                else None
            )
            if docs_pagination and docs_docs_library_id is not None:
                # One opaque token identifies the root pull snapshot.  It is
                # echoed by every table page and is carried inside cursors;
                # no server-side mutable run state is required.
                has_docs_cursor = any(bool(value) for value in docs_cursors.values())
                cursor_tokens: set[str] = set()
                cursor_revisions: set[str] = set()
                if has_docs_cursor:
                    # Every resumable cursor issued by the additive protocol
                    # must carry its opaque token and ACL revision.  A
                    # malformed envelope or a pre-metadata cursor cannot be
                    # safely associated with this root snapshot, so fail
                    # closed instead of silently starting a new run.
                    for value in docs_cursors.values():
                        if not value:
                            continue
                        envelope = pull_cursor_envelope(value)
                        token = envelope.get("snapshot_token") if envelope else None
                        revision = envelope.get("scope_revision") if envelope else None
                        token = _validate_docs_opaque_token(token, "docs_snapshot_token")
                        revision = _validate_docs_opaque_token(
                            revision, "docs_scope_revision"
                        )
                        if token is None or revision is None:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid Docs sync cursor; restart pull",
                            )
                        cursor_tokens.add(token)
                        cursor_revisions.add(revision)
                if len(cursor_tokens) > 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Docs sync snapshot changed; restart pull",
                    )
                if len(cursor_revisions) > 1:
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
                cursor_token = next(iter(cursor_tokens), None)
                if (
                    requested_snapshot_token
                    and cursor_token
                    and requested_snapshot_token != cursor_token
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Docs sync snapshot changed; restart pull",
                    )
                # Preserve the token from a cursor when a legacy client does
                # not echo the new query field.  Cursors issued before the
                # additive token contract are rejected above rather than
                # silently restarting under a different snapshot.
                docs_snapshot_token = (
                    requested_snapshot_token
                    or cursor_token
                    or secrets.token_urlsafe(24)
                )
                cursor_snapshot_token = requested_snapshot_token or cursor_token
                if not has_docs_cursor:
                    cursor_snapshot_token = docs_snapshot_token
                if (
                    docs_scope_revision
                    and current_docs_scope_revision is not None
                    and docs_scope_revision != current_docs_scope_revision
                    and (has_docs_cursor or docs_snapshot_token)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
                if (
                    docs_scope_digest
                    and current_docs_scope_digest is not None
                    and docs_scope_digest != current_docs_scope_digest
                    and has_docs_cursor
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Docs sync scope changed; restart pull",
                    )
            else:
                current_docs_scope_revision = None
            # A v2 client may have a legacy last-pulled timestamp but no
            # additive ACL revision (and sometimes no per-table digest at all).
            # Since cannot safely describe ACL-visible historical rows, so the
            # first revision-less paginated pull must rebuild the visible set.
            # Once the server returns a revision, new clients echo it on
            # continuation and retain the normal since optimization.
            legacy_docs_without_revision = bool(
                docs_pagination and not docs_scope_revision
            )
            force_docs_full = bool(
                docs_pagination
                and current_docs_scope_digest is not None
                and docs_scope_digest is not None
                and docs_scope_digest != current_docs_scope_digest
            ) or legacy_docs_without_revision
            pulled = {}
            for table in requested:
                try:
                    pulled[table] = await _pull_table(
                        table,
                        session,
                        user_id=user_id,
                        project_ids=project_ids,
                        since=since_dt,
                        docs_docs_library_id=docs_docs_library_id,
                        docs_digests=docs_digests,
                        docs_cursors=docs_cursors,
                        docs_pagination=docs_pagination,
                        force_docs_full=force_docs_full,
                        docs_reconcile=docs_reconcile,
                        docs_snapshot_token=cursor_snapshot_token,
                        docs_scope_revision=current_docs_scope_revision,
                        docs_project_id=docs_project_id,
                        docs_accessible_project_ids=docs_scope_project_ids,
                    )
                except (ValueError, TypeError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if table not in DOCS_SYNC_TABLES:
                    pulled[table]["cursor"] = server_time.isoformat()
            response = {
                "tables": pulled,
                "server_time": server_time.isoformat(),
                "has_more": docs_pagination and any(
                    table in DOCS_SYNC_TABLES and payload.get("cursor")
                    for table, payload in pulled.items()
                ),
            }
            if docs_pagination:
                response["docs_pagination_version"] = 2
                response["docs_scope_digest"] = current_docs_scope_digest
                response["docs_snapshot_token"] = docs_snapshot_token
                response["docs_scope_revision"] = current_docs_scope_revision
            if docs_scopes:
                response["docs_scopes"] = docs_scopes
            return response
        finally:
            await session.close()

    @router.post("/push")
    async def push(
        payload: SyncPushPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        results = []
        try:
            for operation in payload.operations:
                try:
                    if operation.table == "projects":
                        entity = await _apply_project_operation(
                            service,
                            session,
                            user_id=user_id,
                            user_info=user_info,
                            operation=operation,
                            workspace_root=workspace_root,
                        )
                    elif operation.table == "tasks":
                        entity = await _apply_task_operation(
                            service, session, user_id=user_id, operation=operation
                        )
                    elif operation.table == "time_entries":
                        entity = await _apply_time_entry_operation(
                            service, session, user_id=user_id, operation=operation
                        )
                    elif operation.table in DOCS_PUSH_TABLES:
                        entity = await _apply_docs_push_operation(
                            session,
                            user_id=user_id,
                            operation=operation,
                            workspace_root=workspace_root,
                        )
                    else:
                        raise TaskManagementError(
                            f"Unsupported push table: {operation.table}",
                            status_code=400,
                        )
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": "ok",
                            "entity": entity,
                            "server_updated_at": entity.get("updated_at")
                            if isinstance(entity, dict)
                            else None,
                        }
                    )
                except (TaskManagementError, DocsOperationError) as exc:
                    await session.rollback()
                    status = "conflict" if exc.status_code == 409 else "error"
                    conflict_entity: Optional[dict[str, Any]] = None
                    # Docs テーブルの entity_id は複合キー（"<node>:<field>" 等）が
                    # あり得るため、UUID として解釈できない場合は None として扱う
                    # （push 全体を 400 で落とさない）。
                    try:
                        entity_id = parse_uuid_or_400(operation.entity_id, "entity_id")
                    except HTTPException:
                        entity_id = None
                    if status == "conflict" and operation.table in DOCS_PUSH_TABLES:
                        docs_service = DocsGraphService(session)
                        conflict_project = operation.payload.get("project_id")
                        if conflict_project:
                            try:
                                docs_workspace = await docs_service.ensure_project_information_library(
                                    UUID(str(conflict_project)), user_id
                                )
                            except Exception:
                                docs_workspace = await docs_service.ensure_library(user_id)
                        else:
                            conflict_ref = operation.payload.get("node_id") or str(
                                operation.entity_id
                            ).split(":", 1)[0]
                            try:
                                conflict_node = await session.get(KnowledgeNode, UUID(str(conflict_ref)))
                            except (TypeError, ValueError):
                                conflict_node = None
                            docs_workspace = (
                                await session.get(DocsLibrary, conflict_node.docs_library_id)
                                if conflict_node is not None
                                else await docs_service.ensure_library(user_id)
                            )
                        conflict_entity = await load_current_docs_entity(
                            session,
                            docs_library_id=docs_workspace.id,
                            table=operation.table,
                            entity_id=operation.entity_id,
                            user_id=user_id,
                        )
                    elif status == "conflict" and entity_id is not None:
                        if operation.table == "tasks":
                            conflict_entity = await _load_current_task_payload(
                                service, session, entity_id
                            )
                        elif operation.table == "time_entries":
                            conflict_entity = await _load_current_time_entry_payload(
                                service, session, entity_id
                            )
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": status,
                            "reason": exc.message,
                            "entity": conflict_entity,
                            "server_updated_at": conflict_entity.get("updated_at")
                            if isinstance(conflict_entity, dict)
                            else None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    logger.exception("Sync push operation failed: %s", operation.op_id)
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": "error",
                            "reason": str(exc),
                        }
                    )
            return {"results": results}
        finally:
            await session.close()

    return router
