"""Sync v3 の行読込（ID指定の一括読込と scope 単位の bootstrap）。

payload は v2 ``/api/sync/pull`` の同テーブルの changes 要素と同じ形にするため、
v2 の serializer・ACL を再利用する（``_pull_*`` の payload 関数、
``serialize_docs_node_for_sync``、``batch_sync_node_access``、
``_conversation_visibility_filter``、``is_participating`` の付与）。
Space は v2 pull に無いため、既存 ``/api/spaces`` 一覧の要素（``to_dict`` + ``can_write``）と同じ形。

状態:
- row: 存在し、compute_scopes のいずれかの scope に含まれる
- missing: 存在しない
- hidden: 存在するが、どの scope にも含まれない
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Sequence
from uuid import UUID

from sqlalchemy import String, and_, bindparam, cast, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import Text

from ..api.docs_sync import (
    serialize_docs_edge,
    serialize_docs_field,
    serialize_docs_field_value,
    serialize_docs_node_for_sync,
    serialize_docs_node_supertag,
    serialize_docs_placement,
    serialize_docs_supertag,
    serialize_docs_supertag_field,
)
from ..api.sync_routes import (
    _CONVERSATION_EVENT_ATTR,
    _conversation_rows_with_event,
    _conversation_session_event_expression,
    _conversation_session_payload,
    _conversation_visibility_filter,
    _project_sync_conversation_statuses,
    _record_field_payload,
    _record_row_payload,
    _record_table_payload,
)
from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    DocsLibrary,
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    Project,
    RecordField,
    RecordRow,
    RecordTable,
    Space,
    Task,
    TaskAssignee,
    TaskOccurrence,
    TaskTag,
    TimeEntry,
)
from ..memory.project_repository import ProjectRepository
from ..services.agent_root_lifecycle import attach_private_root_metadata
from ..services.docs_acl import (
    batch_sync_node_access,
    docs_node_renderable_predicate,
    docs_readable_node_predicate,
)
from .contract import DOCS_TABLES, ENTITY_VERSIONS_SQL, SCOPE_TABLES, SYNC_TABLES, LoadState
from .scopes import compute_scopes

QUERY_CHUNK = 500

LoadResult = tuple[LoadState, "dict[str, Any] | None", int, "list[str]"]
ScopesTuple = tuple[str, list[dict[str, Any]], Any]

_ENTITY_VERSIONS = text(ENTITY_VERSIONS_SQL).bindparams(bindparam("ids", type_=ARRAY(Text)))

_COMPOSITE_COLUMNS: dict[str, tuple[Any, Any]] = {
    "knowledge_node_supertags": (KnowledgeNodeSupertag.node_id, KnowledgeNodeSupertag.supertag_id),
    "knowledge_supertag_fields": (KnowledgeSupertagField.supertag_id, KnowledgeSupertagField.field_id),
    "knowledge_field_values": (KnowledgeFieldValue.node_id, KnowledgeFieldValue.field_id),
}
_SIMPLE_MODELS: dict[str, Any] = {
    "spaces": Space,
    "projects": Project,
    "tasks": Task,
    "task_occurrences": TaskOccurrence,
    "time_entries": TimeEntry,
    "conversation_sessions": ConversationSession,
    "conversation_messages": ConversationMessage,
    "record_tables": RecordTable,
    "record_fields": RecordField,
    "record_rows": RecordRow,
    "knowledge_nodes": KnowledgeNode,
    "knowledge_supertags": KnowledgeSupertag,
    "knowledge_fields": KnowledgeField,
    "knowledge_node_placements": KnowledgeNodePlacement,
    "knowledge_edges": KnowledgeEdge,
}


def _uuid(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _chunks(values: Sequence[str], size: int = QUERY_CHUNK) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _composite_key(*parts: Any) -> str:
    return ":".join(str(part) for part in parts)


def _parse_composite(value: str) -> tuple[UUID, UUID] | None:
    left, sep, right = str(value).partition(":")
    a, b = _uuid(left), _uuid(right)
    if not sep or a is None or b is None:
        return None
    return a, b


def _key_text_expr(table: str) -> Any:
    a, b = _COMPOSITE_COLUMNS[table]
    return func.concat(cast(a, String), literal(":"), cast(b, String)).collate("C")


@dataclass
class _Ctx:
    actor: UUID
    scopes: list[dict[str, Any]]
    project_ids: set[UUID]
    space_access: dict[UUID, bool] = field(default_factory=dict)
    docs_by_library: dict[UUID, list[tuple[UUID | None, str]]] = field(default_factory=dict)
    participating: set[str] | None = None
    libraries: dict[UUID, DocsLibrary] = field(default_factory=dict)

    @classmethod
    def build(cls, actor: UUID, scopes: list[dict[str, Any]], scope_filter: Any) -> "_Ctx":
        ctx = cls(actor=actor, scopes=scopes, project_ids=set(scope_filter.project_ids))
        for scope in scopes:
            if scope["kind"] == "space":
                ctx.space_access[UUID(scope["id"])] = scope["access"] == "write"
            elif scope["kind"] == "docs":
                project_id = _uuid(scope["project_id"]) if scope["project_id"] else None
                ctx.docs_by_library.setdefault(UUID(scope["id"]), []).append(
                    (project_id, scope["scope_key"])
                )
        return ctx

    @property
    def user_key(self) -> str:
        return f"user:{self.actor}"

    async def participating_ids(self, session: AsyncSession) -> set[str]:
        if self.participating is None:
            self.participating = {
                str(pid)
                for pid in await ProjectRepository.get_participating_project_ids(session, self.actor)
            }
        return self.participating

    async def library(self, session: AsyncSession, library_id: UUID) -> DocsLibrary | None:
        if library_id not in self.libraries:
            row = await session.get(DocsLibrary, library_id)
            if row is None:
                return None
            self.libraries[library_id] = row
        return self.libraries[library_id]


# 各 loader は「存在する entity」だけを {entity_id: (payload | None, scope_keys)} で返す。
# scope_keys が空なら hidden、payload は row のときだけ作る。
Found = dict[str, tuple["dict[str, Any] | None", list[str]]]
Loader = Callable[[AsyncSession, _Ctx, list[str]], Awaitable[Found]]


# ---------------------------------------------------------------------------
# Space / Project 系
# ---------------------------------------------------------------------------
async def _load_spaces(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    rows = (await session.execute(select(Space).where(Space.id.in_(uuids)))).scalars().all()
    found: Found = {}
    for row in rows:
        if row.id in ctx.space_access:
            payload = row.to_dict()
            payload["can_write"] = bool(ctx.space_access[row.id])
            found[str(row.id)] = (payload, [f"space:{row.id}"])
        else:
            found[str(row.id)] = (None, [])
    return found


def _project_found(row: Any, project_id: Any, ctx: _Ctx, payload_fn) -> tuple[Any, list[str]]:
    pid = _uuid(project_id)
    if pid is not None and pid in ctx.project_ids:
        return payload_fn(row), [f"project:{pid}"]
    return None, []


async def _load_projects(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    rows = (await session.execute(select(Project).where(Project.id.in_(uuids)))).scalars().all()
    participating = await ctx.participating_ids(session)

    def payload(row: Project) -> dict[str, Any]:
        data = row.to_dict()
        data["is_participating"] = str(data["id"]) in participating
        return data

    return {str(row.id): _project_found(row, row.id, ctx, payload) for row in rows}


async def _load_tasks(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    stmt = (
        select(Task)
        .options(
            selectinload(Task.project),
            selectinload(Task.assignees).selectinload(TaskAssignee.user),
            selectinload(Task.recurrence_rule),
            selectinload(Task.task_tags).selectinload(TaskTag.tag),
        )
        .where(Task.id.in_(uuids))
    )
    rows = (await session.execute(stmt)).scalars().unique().all()
    return {str(row.id): _project_found(row, row.project_id, ctx, lambda r: r.to_dict()) for row in rows}


async def _load_occurrences(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    stmt = (
        select(TaskOccurrence)
        .options(
            selectinload(TaskOccurrence.task).selectinload(Task.project),
            selectinload(TaskOccurrence.task).selectinload(Task.task_tags).selectinload(TaskTag.tag),
        )
        .where(TaskOccurrence.id.in_(uuids))
    )
    rows = (await session.execute(stmt)).scalars().unique().all()
    return {
        str(row.id): _project_found(row, row.task.project_id if row.task else None, ctx, lambda r: r.to_dict())
        for row in rows
    }


async def _load_time_entries(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    stmt = (
        select(TimeEntry)
        .options(
            selectinload(TimeEntry.task).selectinload(Task.project),
            selectinload(TimeEntry.user),
            selectinload(TimeEntry.occurrence),
        )
        .where(TimeEntry.id.in_(uuids))
    )
    rows = (await session.execute(stmt)).scalars().unique().all()
    return {
        str(row.id): _project_found(row, row.task.project_id if row.task else None, ctx, lambda r: r.to_dict())
        for row in rows
    }


async def _load_record_tables(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    rows = (await session.execute(select(RecordTable).where(RecordTable.id.in_(uuids)))).scalars().all()
    return {str(row.id): _project_found(row, row.project_id, ctx, _record_table_payload) for row in rows}


async def _load_record_fields(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    stmt = (
        select(RecordField, RecordTable.project_id)
        .join(RecordTable, RecordTable.id == RecordField.table_id)
        .where(RecordField.id.in_(uuids))
    )
    rows = (await session.execute(stmt)).all()
    return {
        str(row.id): _project_found(row, project_id, ctx, _record_field_payload)
        for row, project_id in rows
    }


async def _load_record_rows(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    rows = (await session.execute(select(RecordRow).where(RecordRow.id.in_(uuids)))).scalars().all()
    return {str(row.id): _project_found(row, row.project_id, ctx, _record_row_payload) for row in rows}


# ---------------------------------------------------------------------------
# 会話（user scope）
# ---------------------------------------------------------------------------
def _conversation_visibility(ctx: _Ctx) -> Any:
    return _conversation_visibility_filter(ctx.actor, sorted(ctx.project_ids, key=str))


async def _load_conversation_sessions(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    existing = set(
        (await session.execute(select(ConversationSession.id).where(ConversationSession.id.in_(uuids))))
        .scalars()
        .all()
    )
    stmt = (
        select(ConversationSession)
        .where(ConversationSession.id.in_(uuids), _conversation_visibility(ctx))
        .add_columns(_conversation_session_event_expression().label(_CONVERSATION_EVENT_ATTR))
    )
    rows = _conversation_rows_with_event(await session.execute(stmt))
    await attach_private_root_metadata(session, rows)
    live = [row for row in rows if row.deleted_at is None]
    projections = await _project_sync_conversation_statuses(
        session,
        [str(row.id) for row in live],
        {str(row.id): row.last_read_at for row in live},
    )
    found: Found = {str(sid): (None, []) for sid in existing}
    for row in rows:
        found[str(row.id)] = (
            _conversation_session_payload(row, status_projection=projections.get(str(row.id))),
            [ctx.user_key],
        )
    return found


async def _load_conversation_messages(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
    uuids = [u for u in map(_uuid, ids) if u is not None]
    existing = set(
        (await session.execute(select(ConversationMessage.id).where(ConversationMessage.id.in_(uuids))))
        .scalars()
        .all()
    )
    stmt = (
        select(ConversationMessage)
        .join(ConversationSession, ConversationMessage.session_id == ConversationSession.id)
        .where(ConversationMessage.id.in_(uuids), _conversation_visibility(ctx))
    )
    rows = (await session.execute(stmt)).scalars().unique().all()
    found: Found = {str(mid): (None, []) for mid in existing}
    for row in rows:
        found[str(row.id)] = (row.to_dict(), [ctx.user_key])
    return found


# ---------------------------------------------------------------------------
# Docs（library × project の scope）
# ---------------------------------------------------------------------------
def docs_visible_statement(
    table: str,
    *,
    library_id: UUID,
    project_id: UUID | None,
    library_owner_id: Any,
    actor: UUID,
) -> Any:
    """v2 ``pull_docs_table`` と同じ可視条件で、その scope に見える行の SELECT を返す。"""
    project_access = docs_readable_node_predicate(
        KnowledgeNode,
        docs_library_id=library_id,
        user_id=actor,
        library_owner_id=library_owner_id,
    )
    if project_id is None:
        project_access = and_(project_access, KnowledgeNode.project_id.is_(None))
    else:
        project_access = and_(project_access, KnowledgeNode.project_id == project_id)
    project_access = and_(project_access, docs_node_renderable_predicate(KnowledgeNode))
    visible_node_ids = select(KnowledgeNode.id).where(
        KnowledgeNode.docs_library_id == library_id, project_access
    )
    visible_tag_ids = select(KnowledgeNodeSupertag.supertag_id).where(
        KnowledgeNodeSupertag.node_id.in_(visible_node_ids)
    )
    visible_field_ids = select(KnowledgeFieldValue.field_id).where(
        KnowledgeFieldValue.node_id.in_(visible_node_ids)
    )
    if table == "knowledge_nodes":
        return select(KnowledgeNode).where(KnowledgeNode.docs_library_id == library_id, project_access)
    if table == "knowledge_supertags":
        return select(KnowledgeSupertag).where(
            KnowledgeSupertag.docs_library_id == library_id,
            KnowledgeSupertag.id.in_(visible_tag_ids),
        )
    if table == "knowledge_fields":
        return select(KnowledgeField).where(
            KnowledgeField.docs_library_id == library_id,
            or_(KnowledgeField.supertag_id.in_(visible_tag_ids), KnowledgeField.id.in_(visible_field_ids)),
        )
    if table == "knowledge_supertag_fields":
        return (
            select(KnowledgeSupertagField)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeSupertagField.field_id)
            .where(
                KnowledgeSupertag.docs_library_id == library_id,
                KnowledgeField.docs_library_id == library_id,
                KnowledgeSupertagField.supertag_id.in_(visible_tag_ids),
            )
        )
    if table == "knowledge_node_supertags":
        return (
            select(KnowledgeNodeSupertag)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
            .where(
                KnowledgeNodeSupertag.node_id.in_(visible_node_ids),
                KnowledgeSupertag.docs_library_id == library_id,
                KnowledgeNodeSupertag.supertag_id.in_(visible_tag_ids),
            )
        )
    if table == "knowledge_field_values":
        return (
            select(KnowledgeFieldValue)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
            .where(
                KnowledgeFieldValue.node_id.in_(visible_node_ids),
                KnowledgeField.docs_library_id == library_id,
                KnowledgeFieldValue.target_node_id.is_(None)
                | KnowledgeFieldValue.target_node_id.in_(visible_node_ids),
            )
        )
    if table == "knowledge_node_placements":
        return select(KnowledgeNodePlacement).where(
            KnowledgeNodePlacement.node_id.in_(visible_node_ids),
            KnowledgeNodePlacement.parent_node_id.in_(visible_node_ids),
        )
    if table == "knowledge_edges":
        return select(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id.in_(visible_node_ids),
            KnowledgeEdge.target_node_id.in_(visible_node_ids),
        )
    raise ValueError(f"not a docs table: {table}")


def _docs_row_key(table: str, row: Any) -> str:
    if table in _COMPOSITE_COLUMNS:
        a, b = _COMPOSITE_COLUMNS[table]
        return _composite_key(getattr(row, a.key), getattr(row, b.key))
    return str(row.id)


def _docs_key_filter(table: str, ids: Sequence[str]) -> Any:
    if table in _COMPOSITE_COLUMNS:
        pairs = [p for p in map(_parse_composite, ids) if p is not None]
        a, b = _COMPOSITE_COLUMNS[table]
        return tuple_(a, b).in_(pairs) if pairs else literal(False)
    uuids = [u for u in map(_uuid, ids) if u is not None]
    return _SIMPLE_MODELS[table].id.in_(uuids)


def _docs_library_statement(table: str, ids: Sequence[str]) -> Any:
    """ACL無しで (key列..., docs_library_id) を引く（存在確認と library 解決）。"""
    key_filter = _docs_key_filter(table, ids)
    if table in ("knowledge_nodes", "knowledge_supertags", "knowledge_fields"):
        model = _SIMPLE_MODELS[table]
        return select(model.id, model.docs_library_id).where(key_filter)
    if table == "knowledge_node_supertags":
        return (
            select(KnowledgeNodeSupertag.node_id, KnowledgeNodeSupertag.supertag_id, KnowledgeNode.docs_library_id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .where(key_filter)
        )
    if table == "knowledge_supertag_fields":
        return (
            select(KnowledgeSupertagField.supertag_id, KnowledgeSupertagField.field_id, KnowledgeSupertag.docs_library_id)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeSupertagField.supertag_id)
            .where(key_filter)
        )
    if table == "knowledge_field_values":
        return (
            select(KnowledgeFieldValue.node_id, KnowledgeFieldValue.field_id, KnowledgeNode.docs_library_id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
            .where(key_filter)
        )
    if table == "knowledge_node_placements":
        return (
            select(KnowledgeNodePlacement.id, KnowledgeNode.docs_library_id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodePlacement.node_id)
            .where(key_filter)
        )
    return (
        select(KnowledgeEdge.id, KnowledgeNode.docs_library_id)
        .join(KnowledgeNode, KnowledgeNode.id == KnowledgeEdge.source_node_id)
        .where(key_filter)
    )


_DOCS_SERIALIZERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "knowledge_supertags": serialize_docs_supertag,
    "knowledge_fields": serialize_docs_field,
    "knowledge_supertag_fields": serialize_docs_supertag_field,
    "knowledge_node_supertags": serialize_docs_node_supertag,
    "knowledge_field_values": serialize_docs_field_value,
    "knowledge_node_placements": serialize_docs_placement,
    "knowledge_edges": serialize_docs_edge,
}


async def _serialize_docs_rows(
    session: AsyncSession, ctx: _Ctx, table: str, library: DocsLibrary, rows: list[Any]
) -> dict[str, dict[str, Any]]:
    if table != "knowledge_nodes":
        serializer = _DOCS_SERIALIZERS[table]
        return {_docs_row_key(table, row): serializer(row) for row in rows}
    access = await batch_sync_node_access(session, rows, library=library, user_id=ctx.actor)
    return {
        str(row.id): await serialize_docs_node_for_sync(
            session, row, library=library, user_id=ctx.actor, access_metadata=access.get(row.id)
        )
        for row in rows
    }


def _make_docs_loader(table: str) -> Loader:
    async def load(session: AsyncSession, ctx: _Ctx, ids: list[str]) -> Found:
        located = (await session.execute(_docs_library_statement(table, ids))).all()
        by_library: dict[UUID, list[str]] = {}
        found: Found = {}
        for row in located:
            *key_parts, library_id = row
            key = _composite_key(*key_parts) if len(key_parts) > 1 else str(key_parts[0])
            found[key] = (None, [])
            if library_id is not None:
                by_library.setdefault(library_id, []).append(key)
        for library_id, keys in by_library.items():
            candidates = ctx.docs_by_library.get(library_id) or []
            if not candidates:
                continue
            library = await ctx.library(session, library_id)
            if library is None:
                continue
            visible_rows: dict[str, Any] = {}
            scope_keys: dict[str, list[str]] = {}
            for project_id, scope_key in candidates:
                stmt = docs_visible_statement(
                    table,
                    library_id=library_id,
                    project_id=project_id,
                    library_owner_id=library.owner_user_id,
                    actor=ctx.actor,
                ).where(_docs_key_filter(table, keys))
                for item in (await session.execute(stmt)).scalars().unique().all():
                    key = _docs_row_key(table, item)
                    visible_rows.setdefault(key, item)
                    scope_keys.setdefault(key, []).append(scope_key)
            payloads = await _serialize_docs_rows(
                session, ctx, table, library, list(visible_rows.values())
            )
            for key, payload in payloads.items():
                found[key] = (payload, sorted(set(scope_keys[key])))
        return found

    return load


_LOADERS: dict[str, Loader] = {
    "spaces": _load_spaces,
    "projects": _load_projects,
    "tasks": _load_tasks,
    "task_occurrences": _load_occurrences,
    "time_entries": _load_time_entries,
    "conversation_sessions": _load_conversation_sessions,
    "conversation_messages": _load_conversation_messages,
    "record_tables": _load_record_tables,
    "record_fields": _load_record_fields,
    "record_rows": _load_record_rows,
    **{table: _make_docs_loader(table) for table in DOCS_TABLES},
}
assert set(_LOADERS) == set(SYNC_TABLES)


async def _versions(session: AsyncSession, table: str, ids: Sequence[str]) -> dict[str, int]:
    result = await session.execute(_ENTITY_VERSIONS, {"table": table, "ids": list(ids)})
    return {str(entity_id): int(version) for entity_id, version in result.all()}


async def _resolve_scopes(
    session: AsyncSession, user_id: UUID | str, scopes: ScopesTuple | None
) -> tuple[UUID, _Ctx]:
    """compute_scopes の結果（無ければ計算）から読込用の文脈を作る。"""
    actor = UUID(str(user_id))
    if scopes is None:
        scopes = await compute_scopes(session, actor)
    _revision, scope_list, scope_filter = scopes
    return actor, _Ctx.build(actor, scope_list, scope_filter)


async def load_rows(
    session: AsyncSession,
    user_id: UUID | str,
    table: str,
    ids: Iterable[str],
    *,
    scopes: ScopesTuple | None = None,
) -> dict[str, LoadResult]:
    """{entity_id: (状態, payload, 版, scope_key一覧)}。

    ``scopes`` は同一トランザクションで計算済みの ``compute_scopes`` の結果（任意）。
    1回の問い合わせは最大 ``QUERY_CHUNK`` 件。
    """
    if table not in _LOADERS:
        raise ValueError(f"unsupported table: {table}")
    _actor, ctx = await _resolve_scopes(session, user_id, scopes)
    unique = list(dict.fromkeys(str(value) for value in ids))
    out: dict[str, LoadResult] = {}
    for chunk in _chunks(unique):
        found = await _LOADERS[table](session, ctx, list(chunk))
        versions = await _versions(session, table, chunk)
        for entity_id in chunk:
            version = versions.get(entity_id, 0)
            if entity_id not in found:
                out[entity_id] = ("missing", None, version, [])
                continue
            payload, scope_keys = found[entity_id]
            if not scope_keys:
                out[entity_id] = ("hidden", None, version, [])
            else:
                out[entity_id] = ("row", payload, version, list(scope_keys))
    return out


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
def _scope_key_statement(ctx: _Ctx, scope: dict[str, Any], table: str) -> tuple[Any, Any, bool]:
    """(key列だけのSELECT, 並び順の列, 複合キーか) を返す。"""
    kind = scope["kind"]
    if table not in SCOPE_TABLES[kind]:
        raise ValueError(f"table {table} is not in scope {scope['scope_key']}")
    if kind == "space":
        return select(Space.id).where(Space.id == UUID(scope["id"])), Space.id, False
    if kind == "project":
        pid = UUID(scope["id"])
        if table == "projects":
            return select(Project.id).where(Project.id == pid), Project.id, False
        if table == "tasks":
            return select(Task.id).where(Task.project_id == pid), Task.id, False
        if table == "task_occurrences":
            return (
                select(TaskOccurrence.id).join(Task, Task.id == TaskOccurrence.task_id).where(Task.project_id == pid),
                TaskOccurrence.id,
                False,
            )
        if table == "time_entries":
            return (
                select(TimeEntry.id).join(Task, Task.id == TimeEntry.task_id).where(Task.project_id == pid),
                TimeEntry.id,
                False,
            )
        if table == "record_tables":
            return select(RecordTable.id).where(RecordTable.project_id == pid), RecordTable.id, False
        if table == "record_fields":
            return (
                select(RecordField.id)
                .join(RecordTable, RecordTable.id == RecordField.table_id)
                .where(RecordTable.project_id == pid),
                RecordField.id,
                False,
            )
        return select(RecordRow.id).where(RecordRow.project_id == pid), RecordRow.id, False
    if kind == "user":
        visibility = _conversation_visibility(ctx)
        if table == "conversation_sessions":
            return select(ConversationSession.id).where(visibility), ConversationSession.id, False
        return (
            select(ConversationMessage.id)
            .join(ConversationSession, ConversationMessage.session_id == ConversationSession.id)
            .where(visibility),
            ConversationMessage.id,
            False,
        )
    # docs
    library_id = UUID(scope["id"])
    library = ctx.libraries.get(library_id)
    project_id = _uuid(scope["project_id"]) if scope["project_id"] else None
    base = docs_visible_statement(
        table,
        library_id=library_id,
        project_id=project_id,
        library_owner_id=getattr(library, "owner_user_id", None),
        actor=ctx.actor,
    )
    if table in _COMPOSITE_COLUMNS:
        key = _key_text_expr(table)
        return base.with_only_columns(key.label("entity_key")), key, True
    model = _SIMPLE_MODELS[table]
    return base.with_only_columns(model.id), model.id, False


async def _scope_and_ctx(
    session: AsyncSession, user_id: UUID | str, scope: dict[str, Any] | str, scopes: ScopesTuple | None
) -> tuple[_Ctx, dict[str, Any], ScopesTuple]:
    if scopes is None:
        scopes = await compute_scopes(session, UUID(str(user_id)))
    _actor, ctx = await _resolve_scopes(session, user_id, scopes)
    scope_key = scope if isinstance(scope, str) else scope["scope_key"]
    match = next((item for item in ctx.scopes if item["scope_key"] == scope_key), None)
    if match is None:
        raise LookupError(f"scope not visible: {scope_key}")
    if match["kind"] == "docs":
        await ctx.library(session, UUID(match["id"]))
    return ctx, match, scopes


async def bootstrap_rows(
    session: AsyncSession,
    user_id: UUID | str,
    scope: dict[str, Any] | str,
    table: str,
    after: str | None,
    limit: int,
    *,
    scopes: ScopesTuple | None = None,
) -> list[tuple[str, dict[str, Any], int]]:
    """scope 内の table の行を主キー昇順（複合キーは entity_id 文字列昇順）で after より後から limit 件。"""
    ctx, match, scopes = await _scope_and_ctx(session, user_id, scope, scopes)
    stmt, order_column, composite = _scope_key_statement(ctx, match, table)
    if after:
        if composite:
            stmt = stmt.where(order_column > after)
        else:
            after_id = _uuid(after)
            if after_id is None:
                raise ValueError("invalid after")
            stmt = stmt.where(order_column > after_id)
    stmt = stmt.order_by(order_column.asc()).limit(int(limit))
    keys = [str(value) for value in (await session.execute(stmt)).scalars().all()]
    if not keys:
        return []
    loaded = await load_rows(session, user_id, table, keys, scopes=scopes)
    rows: list[tuple[str, dict[str, Any], int]] = []
    for key in keys:
        state, payload, version, scope_keys = loaded[key]
        if state == "row" and match["scope_key"] in scope_keys and payload is not None:
            rows.append((key, payload, version))
    return rows


async def count_bootstrap_rows(
    session: AsyncSession,
    user_id: UUID | str,
    scope: dict[str, Any] | str,
    table: str,
    *,
    scopes: ScopesTuple | None = None,
) -> int:
    ctx, match, _scopes = await _scope_and_ctx(session, user_id, scope, scopes)
    stmt, _order, _composite = _scope_key_statement(ctx, match, table)
    return int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
