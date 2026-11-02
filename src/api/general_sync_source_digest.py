"""Fast, migration-free source receipts for general sync projections.

The v1 wire digest in :mod:`general_sync_digest` acknowledges JSON pages and
cannot skip a multi-page table until every page has been read.  This additive
receipt instead fingerprints the MVCC row versions that can affect each
general projection.  It is deliberately separate from the wire digest.

PostgreSQL assigns a new ``xmin`` to every inserted or updated tuple.  A
count plus two independent order-insensitive sums over (primary key, xmin)
therefore changes for inserts, updates, and hard deletes.  Tombstone rows are
not filtered out.  The caller captures a start receipt before a full replay
and acknowledges it only when a terminal-page receipt is identical.  A write
that commits during pagination changes the terminal receipt and forces a
replay on the next run instead of turning application timestamps into a
commit-order cursor.

When a projection or one of its source dependencies changes, bump
``GENERAL_SOURCE_TOKEN_VERSION``.  This conservative source token is scoped
to the authenticated user's accessible project/session set.  It returns only
opaque hashes and aggregate counts; it never returns row values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.sql import literal_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    AgentDialogue,
    AgentDialogueInput,
    AgentPrivateRoot,
    AgentRun,
    ContentDeletionEvent,
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    Project,
    ProjectMember,
    RecordField,
    RecordRow,
    RecordTable,
    Tag,
    Task,
    TaskAssignee,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    User,
)
from ..services.conversation_status import (
    FAILURE_ATTENTION_TTL,
    _AUTO_CLEARING_FAILURE_RUN_STATES,
)


GENERAL_SOURCE_TOKEN_VERSION = 1
GENERAL_SOURCE_TERMINAL_RECEIPTS_VERSION = 1

# Source dependency map for the nine general sync projections.  A change to
# any listed source invalidates the projection; extra dependencies are safe
# false positives, while an omitted dependency could cause a stale skip.
GENERAL_SOURCE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "projects": ("projects", "project_members"),
    "tasks": (
        "projects", "tasks", "task_assignees", "task_recurrence_rules",
        "task_tags", "tags", "users", "content_deletion_events",
    ),
    "task_occurrences": (
        "projects", "tasks", "task_occurrences", "task_tags", "tags",
    ),
    "time_entries": (
        "projects", "tasks", "task_occurrences", "time_entries", "users",
    ),
    "conversation_sessions": (
        "conversation_sessions", "conversation_participants", "agent_private_roots",
        "agent_runs", "conversation_messages", "agent_dialogues",
        "agent_dialogue_inputs",
    ),
    "conversation_messages": (
        "conversation_sessions", "conversation_participants", "conversation_messages",
    ),
    "record_tables": ("record_tables",),
    "record_fields": ("record_tables", "record_fields"),
    "record_rows": ("record_tables", "record_rows"),
}


def _conversation_session_ids(user_id: UUID, project_ids: list[UUID]) -> Any:
    actor_id = str(user_id)
    joined_participant_sessions = select(ConversationParticipant.session_id).where(
        ConversationParticipant.participant_type == "user",
        ConversationParticipant.participant_id == actor_id,
        ConversationParticipant.status == "joined",
    )
    return select(ConversationSession.id).where(
        or_(
            ConversationSession.user_id == actor_id,
            ConversationSession.id.in_(joined_participant_sessions),
        ),
        or_(
            ConversationSession.project_id.is_(None),
            ConversationSession.project_id.in_(project_ids),
        ),
    )


def _source_scopes(user_id: UUID, project_ids: list[UUID]) -> tuple[list[tuple[Any, Any]], dict[str, Any]]:
    actor_id = str(user_id)
    task_ids = select(Task.id).where(Task.project_id.in_(project_ids))
    scoped_record_tables = select(RecordTable.id).where(
        RecordTable.project_id.in_(project_ids)
    )
    visible_sessions = _conversation_session_ids(user_id, project_ids)
    visible_dialogues = select(AgentDialogue.id).where(
        or_(
            AgentDialogue.root_session_id.in_(visible_sessions),
            AgentDialogue.worker_session_id.in_(visible_sessions),
        )
    )
    visible_runs = select(AgentRun.id).where(
        AgentRun.session_id.in_(visible_sessions)
    )

    scopes: list[tuple[Any, Any]] = [
        (Project, Project.id.in_(project_ids)),
        (
            ProjectMember,
            or_(ProjectMember.user_id == user_id, ProjectMember.project_id.in_(project_ids)),
        ),
        (Task, Task.project_id.in_(project_ids)),
        (TaskAssignee, TaskAssignee.task_id.in_(task_ids)),
        (TaskRecurrenceRule, TaskRecurrenceRule.task_id.in_(task_ids)),
        (TaskTag, TaskTag.task_id.in_(task_ids)),
        (
            Tag,
            Tag.id.in_(select(TaskTag.tag_id).where(TaskTag.task_id.in_(task_ids))),
        ),
        (
            User,
            or_(
                User.id == user_id,
                User.id.in_(
                    select(TaskAssignee.user_id).where(TaskAssignee.task_id.in_(task_ids))
                ),
            ),
        ),
        (TaskOccurrence, TaskOccurrence.task_id.in_(task_ids)),
        (TimeEntry, TimeEntry.task_id.in_(task_ids)),
        (ConversationSession, ConversationSession.id.in_(visible_sessions)),
        (
            ConversationParticipant,
            or_(
                ConversationParticipant.session_id.in_(visible_sessions),
                and_(
                    ConversationParticipant.participant_type == "user",
                    ConversationParticipant.participant_id == actor_id,
                ),
            ),
        ),
        (
            AgentPrivateRoot,
            or_(
                AgentPrivateRoot.owner_user_id == user_id,
                AgentPrivateRoot.session_id.in_(visible_sessions),
            ),
        ),
        (AgentRun, AgentRun.session_id.in_(visible_sessions)),
        (ConversationMessage, ConversationMessage.session_id.in_(visible_sessions)),
        (
            AgentDialogue,
            or_(
                AgentDialogue.root_session_id.in_(visible_sessions),
                AgentDialogue.worker_session_id.in_(visible_sessions),
            ),
        ),
        (
            AgentDialogueInput,
            or_(
                AgentDialogueInput.dialogue_id.in_(visible_dialogues),
                AgentDialogueInput.source_run_id.in_(visible_runs),
            ),
        ),
        (
            ContentDeletionEvent,
            or_(
                ContentDeletionEvent.project_id.in_(project_ids),
                ContentDeletionEvent.actor_user_id == user_id,
            ),
        ),
        (RecordTable, RecordTable.project_id.in_(project_ids)),
        (RecordField, RecordField.table_id.in_(scoped_record_tables)),
        (
            RecordRow,
            or_(
                RecordRow.project_id.in_(project_ids),
                RecordRow.table_id.in_(scoped_record_tables),
            ),
        ),
    ]
    return scopes, {"visible_sessions": visible_sessions}


def _max_datetime_expression(values: list[Any]) -> Any:
    normalized = [func.coalesce(value, literal(datetime.min)) for value in values]
    return func.greatest(*normalized)


def _conversation_dependency_event_values() -> list[Any]:
    run_scope = AgentRun.session_id == ConversationSession.id
    dialogue_scope = or_(
        AgentDialogue.root_session_id == ConversationSession.id,
        AgentDialogue.worker_session_id == ConversationSession.id,
    )
    input_scope = or_(
        AgentDialogue.root_session_id == ConversationSession.id,
        and_(
            AgentDialogue.worker_session_id == ConversationSession.id,
            AgentRun.session_id == AgentDialogue.worker_session_id,
        ),
    )

    def aggregate(columns: tuple[Any, ...], *, source: Any, where: Any, joins=()) -> Any:
        statement = select(func.max(_max_datetime_expression(list(columns)))).select_from(source)
        for target, on_clause, isouter in joins:
            statement = statement.join(target, on_clause, isouter=isouter)
        return statement.where(where).correlate(ConversationSession).scalar_subquery()

    return [
        aggregate(
            (AgentRun.created_at, AgentRun.updated_at, AgentRun.last_event_at, AgentRun.ended_at),
            source=AgentRun,
            where=run_scope,
        ),
        aggregate(
            (AgentDialogue.created_at, AgentDialogue.updated_at),
            source=AgentDialogue,
            where=dialogue_scope,
        ),
        aggregate(
            (AgentDialogueInput.created_at, AgentDialogueInput.updated_at),
            source=AgentDialogueInput,
            where=input_scope,
            joins=(
                (AgentDialogue, AgentDialogue.id == AgentDialogueInput.dialogue_id, False),
                (AgentRun, AgentRun.id == AgentDialogueInput.source_run_id, True),
            ),
        ),
        aggregate(
            (ConversationMessage.created_at, ConversationMessage.updated_at, ConversationMessage.deleted_at),
            source=ConversationMessage,
            where=ConversationMessage.session_id == ConversationSession.id,
        ),
    ]


def _conversation_session_event_expression() -> Any:
    values = [
        ConversationSession.last_activity,
        ConversationSession.last_read_at,
        ConversationSession.pin_updated_at,
        ConversationSession.agent_binding_updated_at,
        ConversationSession.session_start,
        ConversationSession.deleted_at,
    ]
    values.extend(_conversation_dependency_event_values())
    return _max_datetime_expression(values)


def _ordered_dependency_json(
    source: Any,
    target: Any,
    values: tuple[Any, ...],
    *,
    where: Any,
    order_by: Any,
    joins: tuple[tuple[Any, Any, bool], ...] = (),
) -> Any:
    """Aggregate only fields read by one payload dependency, in stable order."""

    row_json = cast(func.jsonb_build_array(*values), String())
    statement = select(
        func.string_agg(row_json, aggregate_order_by(literal(","), order_by))
    ).select_from(source)
    for model, on_clause, isouter in joins:
        statement = statement.join(model, on_clause, isouter=isouter)
    return func.coalesce(
        statement.where(where).correlate(target).scalar_subquery(),
        literal(""),
    )


def _general_row_manifest_version(table: str, model: Any, user_id: UUID) -> Any:
    """Build the sole row-version expression used by manifests and row fetches."""

    dependencies: list[Any] = []
    if table == "tasks":
        dependencies.extend((
            select(func.jsonb_build_array(
                Project.name,
                Project.project_metadata["color"].as_string(),
            )).where(Project.id == Task.project_id).correlate(Task).scalar_subquery(),
            _ordered_dependency_json(
                TaskAssignee, Task,
                (TaskAssignee.id, TaskAssignee.task_id, TaskAssignee.user_id,
                 TaskAssignee.is_primary, TaskAssignee.assigned_at,
                 TaskAssignee.assigned_by, User.username, User.display_name),
                where=TaskAssignee.task_id == Task.id,
                order_by=TaskAssignee.id,
                joins=((User, User.id == TaskAssignee.user_id, True),),
            ),
            select(func.jsonb_build_array(
                TaskRecurrenceRule.id, TaskRecurrenceRule.task_id,
                TaskRecurrenceRule.rrule, TaskRecurrenceRule.timezone,
                TaskRecurrenceRule.horizon_days, TaskRecurrenceRule.trigger_status,
                TaskRecurrenceRule.create_new, TaskRecurrenceRule.recur_forever,
                TaskRecurrenceRule.reset_status_to, TaskRecurrenceRule.end_count,
                TaskRecurrenceRule.end_date, TaskRecurrenceRule.skip_weekend,
                TaskRecurrenceRule.skip_holiday, TaskRecurrenceRule.skip_mode,
                TaskRecurrenceRule.created_at, TaskRecurrenceRule.updated_at,
            )).where(TaskRecurrenceRule.task_id == Task.id).correlate(Task).scalar_subquery(),
            _ordered_dependency_json(
                TaskTag, Task,
                (Tag.id, Tag.space_id, Tag.name, Tag.color, Tag.created_by, Tag.created_at),
                where=TaskTag.task_id == Task.id,
                order_by=Tag.id,
                joins=((Tag, Tag.id == TaskTag.tag_id, False),),
            ),
        ))
    elif table == "task_occurrences":
        dependencies.extend((
            select(func.jsonb_build_array(
                Task.project_id, Task.title, Task.auto_close_on_due,
                Project.name, Project.project_metadata["color"].as_string(),
            )).select_from(Task).join(Project, Project.id == Task.project_id, isouter=True)
              .where(Task.id == TaskOccurrence.task_id)
              .correlate(TaskOccurrence).scalar_subquery(),
            _ordered_dependency_json(
                TaskTag, TaskOccurrence,
                (Tag.id, Tag.space_id, Tag.name, Tag.color, Tag.created_by, Tag.created_at),
                where=TaskTag.task_id == TaskOccurrence.task_id,
                order_by=Tag.id,
                joins=((Tag, Tag.id == TaskTag.tag_id, False),),
            ),
        ))
    elif table == "time_entries":
        dependencies.extend((
            select(func.jsonb_build_array(
                User.username, User.display_name,
            )).where(User.id == TimeEntry.user_id).correlate(TimeEntry).scalar_subquery(),
            select(func.jsonb_build_array(
                Task.title, Task.project_id, Project.name,
            )).select_from(Task).join(Project, Project.id == Task.project_id, isouter=True)
              .where(Task.id == TimeEntry.task_id)
              .correlate(TimeEntry).scalar_subquery(),
            select(func.jsonb_build_array(
                TaskOccurrence.start_at, TaskOccurrence.end_at,
            )).where(TaskOccurrence.id == TimeEntry.occurrence_id)
              .correlate(TimeEntry).scalar_subquery(),
        ))
    elif table == "conversation_sessions":
        dependencies.extend((
            _conversation_session_event_expression(),
            select(AgentRun.id).where(
                AgentRun.session_id == ConversationSession.id,
                func.lower(AgentRun.status).in_(
                    tuple(sorted(_AUTO_CLEARING_FAILURE_RUN_STATES))
                ),
                func.coalesce(
                    AgentRun.ended_at,
                    AgentRun.updated_at,
                    AgentRun.created_at,
                ) >= (
                    func.timezone(literal("UTC"), func.now())
                    - literal(FAILURE_ATTENTION_TTL)
                ),
            ).correlate(ConversationSession).exists(),
            _ordered_dependency_json(
                ConversationParticipant, ConversationSession,
                (ConversationParticipant.id, ConversationParticipant.session_id,
                 ConversationParticipant.participant_type,
                 ConversationParticipant.participant_id,
                 ConversationParticipant.display_name, ConversationParticipant.role,
                 ConversationParticipant.status, ConversationParticipant.auto_respond,
                 ConversationParticipant.participant_metadata,
                 ConversationParticipant.created_at, ConversationParticipant.updated_at),
                where=ConversationParticipant.session_id == ConversationSession.id,
                order_by=ConversationParticipant.id,
            ),
            select(func.jsonb_build_array(
                AgentPrivateRoot.id, AgentPrivateRoot.owner_user_id,
                AgentPrivateRoot.purpose, AgentPrivateRoot.agent_id,
                AgentPrivateRoot.session_id, AgentPrivateRoot.state,
                AgentPrivateRoot.created_at, AgentPrivateRoot.updated_at,
            )).where(
                AgentPrivateRoot.session_id == ConversationSession.id,
                AgentPrivateRoot.purpose == "aoidots",
            ).correlate(ConversationSession).scalar_subquery(),
            _ordered_dependency_json(
                AgentRun, ConversationSession,
                (AgentRun.id, AgentRun.status, AgentRun.created_at,
                 AgentRun.updated_at, AgentRun.last_event_at, AgentRun.ended_at),
                where=AgentRun.session_id == ConversationSession.id,
                order_by=AgentRun.id,
            ),
            _ordered_dependency_json(
                AgentDialogue, ConversationSession,
                (AgentDialogue.id, AgentDialogue.state, AgentDialogue.created_at,
                 AgentDialogue.updated_at),
                where=or_(
                    AgentDialogue.root_session_id == ConversationSession.id,
                    AgentDialogue.worker_session_id == ConversationSession.id,
                ),
                order_by=AgentDialogue.id,
            ),
            _ordered_dependency_json(
                AgentDialogueInput, ConversationSession,
                (AgentDialogueInput.id, AgentDialogueInput.dialogue_id,
                 AgentDialogueInput.input_kind, AgentDialogueInput.state,
                 AgentDialogueInput.revision, AgentDialogueInput.answer_message_id,
                 AgentDialogueInput.source_run_id, AgentDialogueInput.created_at,
                 AgentDialogueInput.updated_at),
                where=or_(
                    AgentDialogueInput.dialogue_id.in_(
                        select(AgentDialogue.id).where(or_(
                            AgentDialogue.root_session_id == ConversationSession.id,
                            AgentDialogue.worker_session_id == ConversationSession.id,
                        ))
                    ),
                    AgentDialogueInput.source_run_id.in_(
                        select(AgentRun.id).where(AgentRun.session_id == ConversationSession.id)
                    ),
                ),
                order_by=AgentDialogueInput.id,
            ),
            # xmin changes on each message insert/update, so this is a
            # conservative superset of the payload-field dependency without
            # aggregating large message bodies or metadata. Physical deletes
            # also change the aggregate because the deleted row leaves the set.
            select(func.md5(func.coalesce(func.string_agg(
                cast(ConversationMessage.id, String()) + literal(":")
                + cast(literal_column(
                    f'"{ConversationMessage.__table__.name}".xmin'
                ), String()),
                aggregate_order_by(literal(","), ConversationMessage.id),
            ), literal("")))).where(
                ConversationMessage.session_id == ConversationSession.id,
            ).correlate(ConversationSession).scalar_subquery(),
        ))
    xmin = cast(literal_column(f'"{model.__table__.name}".xmin'), String())
    return func.encode(
        func.int8send(func.hashtextextended(
            cast(func.jsonb_build_array(xmin, *dependencies), String()), literal(0)
        )),
        literal("hex"),
    )


GENERAL_ROW_MANIFEST_TABLES = frozenset({
    "tasks", "task_occurrences", "time_entries", "conversation_sessions",
})


def general_row_manifest_statement(
    table: str,
    user_id: UUID,
    project_ids: list[UUID],
    *,
    row_ids: list[UUID] | None = None,
) -> Any:
    """Select visible row IDs and exact payload-derived row versions.

    The row-version expression is shared with ordinary pages and row-ID fetches
    by routing those lookups through this statement with ``row_ids`` set.
    Tombstone rows remain in the manifest so deletion batches are represented.
    """

    if table not in GENERAL_ROW_MANIFEST_TABLES:
        raise ValueError("Unsupported general row manifest table")
    scope_by_model = {model: scope for model, scope in _source_scopes(user_id, project_ids)[0]}
    model = {
        "tasks": Task,
        "task_occurrences": TaskOccurrence,
        "time_entries": TimeEntry,
        "conversation_sessions": ConversationSession,
    }[table]
    statement = select(
        model.id.label("id"),
        _general_row_manifest_version(table, model, user_id).label("version"),
    ).where(scope_by_model[model])
    if row_ids is not None:
        statement = statement.where(model.id.in_(row_ids))
    return statement.order_by(model.id)


def general_source_digest_statement(user_id: UUID, project_ids: list[UUID]) -> Any:
    """Build one fixed PostgreSQL statement with one small aggregate per source."""

    scopes, _ = _source_scopes(user_id, project_ids)
    branches = []
    for model, scope in scopes:
        table = model.__table__
        key_parts = [cast(column, String()) for column in table.primary_key.columns]
        key_parts.append(cast(literal_column("xmin"), String()))
        row_version_key = cast(func.jsonb_build_array(*key_parts), String())
        branches.append(
            select(
                literal(table.name).label("source_table"),
                func.count().label("row_count"),
                func.coalesce(
                    func.sum(func.hashtextextended(row_version_key, literal(0))), 0
                ).label("hash_a"),
                func.coalesce(
                    func.sum(func.hashtextextended(row_version_key, literal(1))), 0
                ).label("hash_b"),
            ).select_from(table).where(scope)
        )
    return union_all(*branches).order_by(literal_column("source_table"))


async def calculate_general_source_digests(
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    scope_fingerprint: str,
) -> dict[str, str]:
    """Return per-output opaque source receipts in one SQL round trip."""

    rows = (await session.execute(general_source_digest_statement(user_id, project_ids))).all()
    source_material = {
        str(row.source_table): [
            int(row.row_count),
            str(row.hash_a),
            str(row.hash_b),
        ]
        for row in rows
    }
    digests: dict[str, str] = {}
    for table, dependencies in GENERAL_SOURCE_DEPENDENCIES.items():
        material = {
            "version": GENERAL_SOURCE_TOKEN_VERSION,
            "scope": scope_fingerprint,
            "table": table,
            "sources": [
                [name, *source_material.get(name, [0, "0", "0"])]
                for name in dependencies
            ],
        }
        if table == "conversation_sessions":
            # Row manifests derive the active failure marker at query time;
            # roll its receipt daily so clients re-evaluate after the 24-hour TTL.
            material["attention_day"] = datetime.now(timezone.utc).date().isoformat()
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        digests[table] = hashlib.sha256(encoded).hexdigest()
    return digests


def valid_general_source_digests(value: Any, tables: set[str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(table): str(digest)
        for table, digest in value.items()
        if table in tables
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    }
