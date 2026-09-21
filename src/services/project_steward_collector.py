"""Bounded, Project-owner-scoped evidence collection for Project Steward.

This module is deliberately read-only.  It does not call an LLM, update a
Heartbeat cursor, create conversation rows, or perform Project Steward writes.
The caller may persist ``next_cursor`` only after the complete collector call
returns successfully.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import and_, case, false, func, or_, select, true

from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    KnowledgeNode,
    KnowledgeRevision,
    Project,
    ProjectKnowledgeRef,
    Task,
    TaskActivity,
)
from .docs_acl import docs_readable_node_predicate
from .docs_graph_service import docs_searchable_body_text


SOURCE_CHAT = "chat"
SOURCE_TASKS = "tasks"
SOURCE_DOCS = "docs"
SOURCE_NAMES = (SOURCE_CHAT, SOURCE_TASKS, SOURCE_DOCS)

BATCH_SIZE = 200

MAX_CHAT_CONTENT_CHARS = 4000
MAX_TASK_TITLE_CHARS = 500
MAX_TASK_DESCRIPTION_CHARS = 2000
MAX_DOC_TITLE_CHARS = 500
MAX_DOC_BODY_CHARS = 4000
MAX_CHANGE_SUMMARY_CHARS = 1000
MAX_JSON_STRING_CHARS = 1000
MAX_JSON_LIST_ITEMS = 32
MAX_JSON_DICT_ITEMS = 32
MAX_JSON_DEPTH = 4

_SECRET_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
)


class ProjectStewardCollectorError(RuntimeError):
    """Collector input/state cannot be handled safely."""


class ProjectStewardScopeError(ProjectStewardCollectorError):
    """Requested Project is not inside the exact owner scope."""


@dataclass(frozen=True)
class _CursorPosition:
    changed_at: datetime
    event_id: str

    @property
    def kind(self) -> str:
        return self.event_id.split(":", 1)[0]


@dataclass(frozen=True)
class _Event:
    kind: str
    event_id: str
    changed_at: datetime
    row: Any


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectStewardCollectorError(f"invalid {field}") from exc


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _naive_utc(value).isoformat()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _is_secret_key(value: Any) -> bool:
    key = str(value or "").strip().casefold()
    return any(fragment in key for fragment in _SECRET_KEY_FRAGMENTS)


def _bounded_public_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_JSON_DEPTH:
        return None

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _clip(value, MAX_JSON_STRING_CHARS)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(
            sorted(value.items(), key=lambda pair: str(pair[0]))
        ):
            if index >= MAX_JSON_DICT_ITEMS:
                break
            if _is_secret_key(key):
                continue
            result[str(key)[:120]] = _bounded_public_json(
                item,
                depth=depth + 1,
            )
        return result

    if isinstance(value, (list, tuple)):
        return [
            _bounded_public_json(item, depth=depth + 1)
            for item in list(value)[:MAX_JSON_LIST_ITEMS]
        ]

    return _clip(value, MAX_JSON_STRING_CHARS)


def _cursor_payload(position: _CursorPosition | None) -> dict[str, str] | None:
    if position is None:
        return None
    return {
        "changed_at": _iso(position.changed_at) or "",
        "id": position.event_id,
    }


def _parse_cursor_position(
    value: Any,
    *,
    allowed_kinds: frozenset[str],
) -> _CursorPosition | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProjectStewardCollectorError("invalid collector cursor")

    raw_changed_at = str(value.get("changed_at") or "").strip()
    event_id = str(value.get("id") or "").strip()
    if not raw_changed_at or not event_id or ":" not in event_id:
        raise ProjectStewardCollectorError("invalid collector cursor")

    kind = event_id.split(":", 1)[0]
    if kind not in allowed_kinds:
        raise ProjectStewardCollectorError("invalid collector cursor kind")

    try:
        changed_at = datetime.fromisoformat(
            raw_changed_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ProjectStewardCollectorError(
            "invalid collector cursor timestamp"
        ) from exc

    changed_at = _naive_utc(changed_at)
    _cursor_uuid(event_id, expected_kind=kind)
    return _CursorPosition(changed_at=changed_at, event_id=event_id)


def _cursor_uuid(event_id: str, *, expected_kind: str) -> uuid.UUID:
    prefix = f"{expected_kind}:"
    if not event_id.startswith(prefix):
        raise ProjectStewardCollectorError("invalid collector cursor id")

    raw = event_id[len(prefix) :]
    if expected_kind == "task":
        raw = raw.split("@", 1)[0]

    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ProjectStewardCollectorError(
            "invalid collector cursor id"
        ) from exc


def _after_cursor_condition(
    *,
    changed_at_column: Any,
    id_column: Any,
    kind: str,
    cursor: _CursorPosition | None,
) -> Any:
    """Build `(changed_at, event_id) > cursor` without cross-table ID casts."""

    if cursor is None:
        return true()

    if kind > cursor.kind:
        equal_time_condition = true()
    elif kind < cursor.kind:
        equal_time_condition = false()
    else:
        cursor_uuid = _cursor_uuid(
            cursor.event_id,
            expected_kind=kind,
        )
        equal_time_condition = id_column > cursor_uuid

    return or_(
        changed_at_column > cursor.changed_at,
        and_(
            changed_at_column == cursor.changed_at,
            equal_time_condition,
        ),
    )


def _event_sort_key(event: _Event) -> tuple[datetime, str]:
    return (event.changed_at, event.event_id)


def _message_changed_at_column() -> Any:
    edited_at = func.coalesce(
        ConversationMessage.updated_at,
        ConversationMessage.created_at,
    )
    return case(
        (
            and_(
                ConversationMessage.deleted_at.is_not(None),
                ConversationMessage.deleted_at > edited_at,
            ),
            ConversationMessage.deleted_at,
        ),
        else_=edited_at,
    )


def _node_changed_at_column() -> Any:
    updated_at = func.coalesce(
        KnowledgeNode.updated_at,
        KnowledgeNode.created_at,
    )
    return case(
        (
            and_(
                KnowledgeNode.archived_at.is_not(None),
                KnowledgeNode.archived_at > updated_at,
            ),
            KnowledgeNode.archived_at,
        ),
        else_=updated_at,
    )


def _message_event_type(row: ConversationMessage) -> str:
    if row.deleted_at is not None:
        return "deleted"
    if (
        row.updated_at is not None
        and row.created_at is not None
        and row.updated_at > row.created_at
    ):
        return "edited"
    return "created"


def _task_snapshot(row: Task | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "title": _clip(row.title, MAX_TASK_TITLE_CHARS),
        "description": _clip(
            row.description,
            MAX_TASK_DESCRIPTION_CHARS,
        ),
        "status": row.status,
        "priority": row.priority,
        "start_at": _iso(row.start_at),
        "end_at": _iso(row.end_at),
        "completed_at": _iso(row.completed_at),
        "archived_at": _iso(row.archived_at),
        "deleted_at": _iso(row.deleted_at),
        "updated_at": _iso(row.updated_at),
        "parent_task_id": (
            str(row.parent_task_id)
            if row.parent_task_id is not None
            else None
        ),
    }


def _docs_node_snapshot(row: KnowledgeNode | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "project_id": (
            str(row.project_id)
            if row.project_id is not None
            else None
        ),
        "parent_id": (
            str(row.parent_id)
            if row.parent_id is not None
            else None
        ),
        "root_page_id": (
            str(row.root_page_id)
            if row.root_page_id is not None
            else None
        ),
        "system_key": _clip(row.system_key, 500),
        "node_type": row.node_type,
        "title": _clip(row.title, MAX_DOC_TITLE_CHARS),
        "body_text": _clip(
            docs_searchable_body_text(
                row.body_text,
                row.body_json,
            ),
            MAX_DOC_BODY_CHARS,
        ),
        "updated_at": _iso(row.updated_at),
        "archived_at": _iso(row.archived_at),
    }


class ProjectStewardCollector:
    """Collect deterministic evidence from one active owner-scoped Project."""

    def __init__(
        self,
        session: Any,
        *,
        batch_size: int = BATCH_SIZE,
    ):
        parsed_batch_size = int(batch_size)
        if parsed_batch_size < 1 or parsed_batch_size > BATCH_SIZE:
            raise ValueError(
                f"batch_size must be between 1 and {BATCH_SIZE}"
            )
        self._session = session
        self._batch_size = parsed_batch_size

    async def collect(
        self,
        *,
        project_id: str | uuid.UUID,
        owner_user_id: str | uuid.UUID,
        cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return evidence and an all-sources-success next cursor.

        ``cursor`` is never mutated.  Any source failure raises and therefore
        returns no ``next_cursor`` for the caller to persist.
        """

        project_uuid = _uuid(project_id, field="project_id")
        owner_uuid = _uuid(owner_user_id, field="owner_user_id")
        project = await self._require_owner_project(
            project_uuid,
            owner_uuid,
        )

        input_cursor = copy.deepcopy(dict(cursor or {}))
        unknown_sources = set(input_cursor) - set(SOURCE_NAMES)
        if unknown_sources:
            raise ProjectStewardCollectorError(
                "unknown Project Steward cursor source"
            )

        chat_cursor = _parse_cursor_position(
            input_cursor.get(SOURCE_CHAT),
            allowed_kinds=frozenset({"chat"}),
        )
        task_cursor = _parse_cursor_position(
            input_cursor.get(SOURCE_TASKS),
            allowed_kinds=frozenset({"task", "task_activity"}),
        )
        docs_cursor = _parse_cursor_position(
            input_cursor.get(SOURCE_DOCS),
            allowed_kinds=frozenset(
                {
                    "docs_node",
                    "docs_revision",
                    "project_knowledge_ref",
                }
            ),
        )

        chat_evidence, next_chat = await self._collect_chat(
            project.id,
            chat_cursor,
        )
        task_evidence, next_tasks = await self._collect_tasks(
            project.id,
            task_cursor,
        )
        docs_evidence, next_docs = await self._collect_docs(
            project,
            owner_uuid,
            docs_cursor,
        )

        return {
            "project_id": str(project.id),
            "evidence": {
                SOURCE_CHAT: chat_evidence,
                SOURCE_TASKS: task_evidence,
                SOURCE_DOCS: docs_evidence,
            },
            "next_cursor": {
                SOURCE_CHAT: _cursor_payload(next_chat),
                SOURCE_TASKS: _cursor_payload(next_tasks),
                SOURCE_DOCS: _cursor_payload(next_docs),
            },
        }

    async def _require_owner_project(
        self,
        project_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Project:
        project = await self._session.scalar(
            select(Project)
            .where(
                Project.id == project_id,
                Project.owner_id == owner_user_id,
                Project.deleted_at.is_(None),
            )
            .limit(1)
        )
        if project is None:
            raise ProjectStewardScopeError(
                "Project is outside Project Automation owner scope"
            )
        return project

    async def _collect_chat(
        self,
        project_id: uuid.UUID,
        initial_cursor: _CursorPosition | None,
    ) -> tuple[list[dict[str, Any]], _CursorPosition | None]:
        evidence: list[dict[str, Any]] = []
        position = initial_cursor
        changed_at_column = _message_changed_at_column()

        while True:
            statement = (
                select(
                    ConversationMessage,
                    changed_at_column.label("steward_changed_at"),
                )
                .join(
                    ConversationSession,
                    ConversationSession.id
                    == ConversationMessage.session_id,
                )
                .where(
                    ConversationSession.project_id == project_id,
                    _after_cursor_condition(
                        changed_at_column=changed_at_column,
                        id_column=ConversationMessage.id,
                        kind="chat",
                        cursor=position,
                    ),
                )
                .order_by(
                    changed_at_column.asc(),
                    ConversationMessage.id.asc(),
                )
                .limit(self._batch_size)
            )
            rows = (await self._session.execute(statement)).all()
            if not rows:
                break

            events: list[_Event] = []
            for row, changed_at in rows:
                if changed_at is None:
                    continue
                events.append(
                    _Event(
                        kind="chat",
                        event_id=f"chat:{row.id}",
                        changed_at=_naive_utc(changed_at),
                        row=row,
                    )
                )

            events.sort(key=_event_sort_key)
            if not events:
                break

            for event in events:
                row = event.row
                evidence.append(
                    {
                        "source": SOURCE_CHAT,
                        "kind": "message",
                        "evidence_id": event.event_id,
                        "changed_at": _iso(event.changed_at),
                        "event_type": _message_event_type(row),
                        "session_id": str(row.session_id),
                        "message_id": str(row.id),
                        "role": row.role,
                        "content": _clip(
                            row.content,
                            MAX_CHAT_CONTENT_CHARS,
                        ),
                        "sender_type": row.sender_type,
                        "sender_display_name": _clip(
                            row.sender_display_name,
                            200,
                        ),
                        "created_at": _iso(row.created_at),
                        "edited_at": _iso(row.updated_at),
                        "deleted_at": _iso(row.deleted_at),
                    }
                )

            last = events[-1]
            position = _CursorPosition(
                changed_at=last.changed_at,
                event_id=last.event_id,
            )

        return evidence, position

    async def _collect_tasks(
        self,
        project_id: uuid.UUID,
        initial_cursor: _CursorPosition | None,
    ) -> tuple[list[dict[str, Any]], _CursorPosition | None]:
        evidence: list[dict[str, Any]] = []
        position = initial_cursor

        while True:
            activity_statement = (
                select(TaskActivity)
                .join(Task, Task.id == TaskActivity.task_id)
                .where(
                    Task.project_id == project_id,
                    _after_cursor_condition(
                        changed_at_column=TaskActivity.created_at,
                        id_column=TaskActivity.id,
                        kind="task_activity",
                        cursor=position,
                    ),
                )
                .order_by(
                    TaskActivity.created_at.asc(),
                    TaskActivity.id.asc(),
                )
                .limit(self._batch_size)
            )
            task_statement = (
                select(Task)
                .where(
                    Task.project_id == project_id,
                    _after_cursor_condition(
                        changed_at_column=Task.updated_at,
                        id_column=Task.id,
                        kind="task",
                        cursor=position,
                    ),
                )
                .order_by(
                    Task.updated_at.asc(),
                    Task.id.asc(),
                )
                .limit(self._batch_size)
            )

            activities = list(
                (
                    await self._session.execute(activity_statement)
                )
                .scalars()
                .all()
            )
            tasks = list(
                (
                    await self._session.execute(task_statement)
                )
                .scalars()
                .all()
            )

            events: list[_Event] = []
            for activity in activities:
                if activity.created_at is None:
                    continue
                events.append(
                    _Event(
                        kind="task_activity",
                        event_id=f"task_activity:{activity.id}",
                        changed_at=_naive_utc(activity.created_at),
                        row=activity,
                    )
                )
            for task in tasks:
                if task.updated_at is None:
                    continue
                changed_at = _naive_utc(task.updated_at)
                events.append(
                    _Event(
                        kind="task",
                        event_id=(
                            f"task:{task.id}@{changed_at.isoformat()}"
                        ),
                        changed_at=changed_at,
                        row=task,
                    )
                )

            events.sort(key=_event_sort_key)
            events = events[: self._batch_size]
            if not events:
                break

            task_ids = {
                (
                    event.row.task_id
                    if event.kind == "task_activity"
                    else event.row.id
                )
                for event in events
            }
            snapshots = {
                task.id: task
                for task in (
                    (
                        await self._session.execute(
                            select(Task).where(
                                Task.project_id == project_id,
                                Task.id.in_(task_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }

            for event in events:
                if event.kind == "task_activity":
                    activity = event.row
                    snapshot = snapshots.get(activity.task_id)
                    if snapshot is None:
                        raise ProjectStewardCollectorError(
                            "Project task activity lost its Project snapshot"
                        )
                    evidence.append(
                        {
                            "source": SOURCE_TASKS,
                            "kind": "task_activity",
                            "evidence_id": event.event_id,
                            "changed_at": _iso(event.changed_at),
                            "task_id": str(activity.task_id),
                            "activity_type": activity.activity_type,
                            "payload": _bounded_public_json(
                                activity.payload or {}
                            ),
                            "task": _task_snapshot(snapshot),
                        }
                    )
                else:
                    task = event.row
                    snapshot = snapshots.get(task.id)
                    if snapshot is None:
                        raise ProjectStewardCollectorError(
                            "Project task snapshot disappeared"
                        )
                    evidence.append(
                        {
                            "source": SOURCE_TASKS,
                            "kind": "task_snapshot",
                            "evidence_id": event.event_id,
                            "changed_at": _iso(event.changed_at),
                            "task_id": str(task.id),
                            "task": _task_snapshot(snapshot),
                        }
                    )

            last = events[-1]
            position = _CursorPosition(
                changed_at=last.changed_at,
                event_id=last.event_id,
            )

        return evidence, position

    async def _authorized_project_reference_node_ids(
        self,
        project_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """Resolve current readable ProjectKnowledgeRef targets in bounded batches."""

        authorized_ids: set[uuid.UUID] = set()
        last_ref_id: uuid.UUID | None = None

        while True:
            statement = (
                select(
                    ProjectKnowledgeRef.id,
                    ProjectKnowledgeRef.knowledge_node_id,
                    KnowledgeNode.docs_library_id,
                )
                .join(
                    KnowledgeNode,
                    KnowledgeNode.id
                    == ProjectKnowledgeRef.knowledge_node_id,
                )
                .where(
                    ProjectKnowledgeRef.project_id == project_id,
                )
                .order_by(ProjectKnowledgeRef.id.asc())
                .limit(self._batch_size)
            )
            if last_ref_id is not None:
                statement = statement.where(
                    ProjectKnowledgeRef.id > last_ref_id
                )

            rows = (await self._session.execute(statement)).all()
            if not rows:
                break

            node_ids_by_library: dict[uuid.UUID, list[uuid.UUID]] = {}
            for _, node_id, docs_library_id in rows:
                node_ids_by_library.setdefault(
                    docs_library_id,
                    [],
                ).append(node_id)

            for docs_library_id, node_ids in node_ids_by_library.items():
                visible_result = await self._session.execute(
                    select(KnowledgeNode.id).where(
                        KnowledgeNode.id.in_(node_ids),
                        KnowledgeNode.archived_at.is_(None),
                        docs_readable_node_predicate(
                            KnowledgeNode,
                            docs_library_id=docs_library_id,
                            user_id=owner_user_id,
                            required="read",
                        ),
                    )
                )
                authorized_ids.update(visible_result.scalars().all())

            last_ref_id = rows[-1][0]

        return authorized_ids

    async def _collect_docs(
        self,
        project: Project,
        owner_user_id: uuid.UUID,
        initial_cursor: _CursorPosition | None,
    ) -> tuple[list[dict[str, Any]], _CursorPosition | None]:
        evidence: list[dict[str, Any]] = []
        position = initial_cursor
        project_id = project.id
        node_changed_at = _node_changed_at_column()

        project_node_ids = select(KnowledgeNode.id).where(
            KnowledgeNode.project_id == project_id
        )
        authorized_referenced_node_ids = (
            await self._authorized_project_reference_node_ids(
                project_id,
                owner_user_id,
            )
        )

        while True:
            node_statement = (
                select(
                    KnowledgeNode,
                    node_changed_at.label("steward_changed_at"),
                )
                .where(
                    KnowledgeNode.project_id == project_id,
                    _after_cursor_condition(
                        changed_at_column=node_changed_at,
                        id_column=KnowledgeNode.id,
                        kind="docs_node",
                        cursor=position,
                    ),
                )
                .order_by(
                    node_changed_at.asc(),
                    KnowledgeNode.id.asc(),
                )
                .limit(self._batch_size)
            )
            revision_statement = (
                select(KnowledgeRevision)
                .where(
                    or_(
                        KnowledgeRevision.node_id.in_(project_node_ids),
                        KnowledgeRevision.node_id.in_(
                            authorized_referenced_node_ids
                        ),
                    ),
                    _after_cursor_condition(
                        changed_at_column=KnowledgeRevision.created_at,
                        id_column=KnowledgeRevision.id,
                        kind="docs_revision",
                        cursor=position,
                    ),
                )
                .order_by(
                    KnowledgeRevision.created_at.asc(),
                    KnowledgeRevision.id.asc(),
                )
                .limit(self._batch_size)
            )
            ref_statement = (
                select(ProjectKnowledgeRef)
                .where(
                    ProjectKnowledgeRef.project_id == project_id,
                    ProjectKnowledgeRef.knowledge_node_id.in_(
                        authorized_referenced_node_ids
                    ),
                    _after_cursor_condition(
                        changed_at_column=ProjectKnowledgeRef.updated_at,
                        id_column=ProjectKnowledgeRef.id,
                        kind="project_knowledge_ref",
                        cursor=position,
                    ),
                )
                .order_by(
                    ProjectKnowledgeRef.updated_at.asc(),
                    ProjectKnowledgeRef.id.asc(),
                )
                .limit(self._batch_size)
            )

            node_rows = (await self._session.execute(node_statement)).all()
            revisions = list(
                (
                    await self._session.execute(revision_statement)
                )
                .scalars()
                .all()
            )
            refs = list(
                (
                    await self._session.execute(ref_statement)
                )
                .scalars()
                .all()
            )

            events: list[_Event] = []
            for node, changed_at in node_rows:
                if changed_at is None:
                    continue
                events.append(
                    _Event(
                        kind="docs_node",
                        event_id=f"docs_node:{node.id}",
                        changed_at=_naive_utc(changed_at),
                        row=node,
                    )
                )
            for revision in revisions:
                if revision.created_at is None:
                    continue
                events.append(
                    _Event(
                        kind="docs_revision",
                        event_id=f"docs_revision:{revision.id}",
                        changed_at=_naive_utc(revision.created_at),
                        row=revision,
                    )
                )
            for ref in refs:
                if ref.updated_at is None:
                    continue
                events.append(
                    _Event(
                        kind="project_knowledge_ref",
                        event_id=(
                            f"project_knowledge_ref:{ref.id}"
                        ),
                        changed_at=_naive_utc(ref.updated_at),
                        row=ref,
                    )
                )

            events.sort(key=_event_sort_key)
            events = events[: self._batch_size]
            if not events:
                break

            referenced_ids = {
                event.row.knowledge_node_id
                for event in events
                if event.kind == "project_knowledge_ref"
            }
            referenced_snapshots: dict[uuid.UUID, KnowledgeNode] = {}
            if referenced_ids:
                referenced_snapshots = {
                    node.id: node
                    for node in (
                        (
                            await self._session.execute(
                                select(KnowledgeNode).where(
                                    KnowledgeNode.id.in_(
                                        referenced_ids
                                    )
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                }

            for event in events:
                if event.kind == "docs_node":
                    node = event.row
                    evidence.append(
                        {
                            "source": SOURCE_DOCS,
                            "kind": "knowledge_node",
                            "evidence_id": event.event_id,
                            "changed_at": _iso(event.changed_at),
                            "event_type": (
                                "archived"
                                if node.archived_at is not None
                                else "updated"
                            ),
                            "node": _docs_node_snapshot(node),
                        }
                    )
                elif event.kind == "docs_revision":
                    revision = event.row
                    evidence.append(
                        {
                            "source": SOURCE_DOCS,
                            "kind": "knowledge_revision",
                            "evidence_id": event.event_id,
                            "changed_at": _iso(event.changed_at),
                            "revision_id": str(revision.id),
                            "node_id": str(revision.node_id),
                            "title": _clip(
                                revision.title,
                                MAX_DOC_TITLE_CHARS,
                            ),
                            "body_text": _clip(
                                docs_searchable_body_text(
                                    revision.body_text,
                                    revision.body_json,
                                ),
                                MAX_DOC_BODY_CHARS,
                            ),
                            "change_summary": _clip(
                                revision.change_summary,
                                MAX_CHANGE_SUMMARY_CHARS,
                            ),
                            "source_refs": _bounded_public_json(
                                revision.source_refs_json or []
                            ),
                            "created_at": _iso(
                                revision.created_at
                            ),
                        }
                    )
                else:
                    ref = event.row
                    referenced_node = referenced_snapshots.get(
                        ref.knowledge_node_id
                    )
                    if referenced_node is None:
                        raise ProjectStewardCollectorError(
                            "ProjectKnowledgeRef target disappeared"
                        )
                    evidence.append(
                        {
                            "source": SOURCE_DOCS,
                            "kind": "project_knowledge_ref",
                            "evidence_id": event.event_id,
                            "changed_at": _iso(event.changed_at),
                            "reference_id": str(ref.id),
                            "knowledge_node_id": str(
                                ref.knowledge_node_id
                            ),
                            "relation_type": ref.relation_type,
                            "priority": ref.priority,
                            "updated_at": _iso(ref.updated_at),
                            "node": _docs_node_snapshot(
                                referenced_node
                            ),
                        }
                    )

            last = events[-1]
            position = _CursorPosition(
                changed_at=last.changed_at,
                event_id=last.event_id,
            )

        return evidence, position


__all__ = [
    "BATCH_SIZE",
    "ProjectStewardCollector",
    "ProjectStewardCollectorError",
    "ProjectStewardScopeError",
]
