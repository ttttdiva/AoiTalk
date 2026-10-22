"""Batch projection of the user-facing conversation history status.

The history filter needs one state that is shared by the Web and native
clients.  The state is deliberately derived at read time from the existing
conversation/run/dialogue rows; it is not a second lifecycle column on a
session.  ``project_conversation_statuses`` performs a bounded number of
queries for the complete visible page and never performs a query per
conversation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal
import uuid

from sqlalchemy import (
    Boolean,
    Numeric,
    String,
    and_,
    case,
    cast,
    desc,
    false,
    func,
    literal,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from ..memory.database import get_database_manager
from ..memory.models import (
    AgentDialogue,
    AgentDialogueInput,
    AgentRun,
    ConversationMessage,
)


ConversationStatus = Literal["idle", "ready", "needs_attention", "working"]


@dataclass(frozen=True, slots=True)
class ConversationStatusProjection:
    """Status plus the latest completed assistant fence, if one exists."""

    status: ConversationStatus
    history_completed_at: datetime | None = None

_ACTIVE_RUN_STATES = frozenset({"queued", "running"})
_FAILURE_RUN_STATES = frozenset(
    {"failed", "uncertain", "blocked", "awaiting_approval"}
)
_TERMINAL_DIALOGUE_STATES_EXCLUDING_PENDING = frozenset({"cancelled", "expired"})
_IGNORED_ASSISTANT_GENERATION_STATES = frozenset(
    {"cancelled", "failed", "error", "partial", "input_pending", "awaiting_input"}
)
_ASSISTANT_COMPLETION_MARKERS = (
    "agent_dialogue_input_id",
    "dialogue_input_id",
    "input_id",
    "agent_question",
    "human_question",
    "root_control_turn",
    "root_agent_control_relay",
    "agent_dialogue_control_relay",
    "agent_control_actor",
    "source_human_control",
    "budget_blocked",
    "restart_recovery_state",
    "safe_error_code",
)


class _JsonPathType(FunctionElement):
    """Dialect-neutral JSON path type probe used by the status SQL."""

    type = String()
    # ``path`` is stored on this small custom expression rather than in its
    # SQLAlchemy clauses, so sharing a compiled cache entry could reuse the
    # wrong JSON path.  The status query is already bounded by batch size.
    inherit_cache = False

    def __init__(self, column: Any, path: Sequence[str]):
        self.column = column
        self.path = tuple(path)
        super().__init__()


def _json_path_sql_path(path: Sequence[str]) -> str:
    # All current paths are server-owned identifiers.  Keep the quoting here
    # explicit so a future path containing punctuation remains a JSON path.
    return "$" + "".join(f'."{part.replace(chr(34), chr(34) * 2)}"' for part in path)


@compiles(_JsonPathType, "sqlite")
def _compile_json_path_type_sqlite(
    element: _JsonPathType, compiler: Any, **kwargs: Any
) -> str:
    column = compiler.process(element.column, **kwargs)
    path = compiler.process(literal(_json_path_sql_path(element.path)), **kwargs)
    return f"json_type({column}, {path})"


@compiles(_JsonPathType, "postgresql")
def _compile_json_path_type_postgresql(
    element: _JsonPathType, compiler: Any, **kwargs: Any
) -> str:
    expression = compiler.process(element.column, **kwargs)
    for part in element.path:
        key = compiler.process(literal(part), **kwargs)
        expression = f"({expression} -> {key})"
    return f"jsonb_typeof({expression})"


@compiles(_JsonPathType)
def _compile_json_path_type_default(
    element: _JsonPathType, compiler: Any, **kwargs: Any
) -> str:
    column = compiler.process(element.column, **kwargs)
    path = compiler.process(literal(_json_path_sql_path(element.path)), **kwargs)
    return f"json_type({column}, {path})"


def _json_path_value(column: Any, path: Sequence[str]) -> Any:
    """Return a scalar/textual JSON path expression without JSON casting."""

    expression = column
    for part in path:
        expression = expression[part]
    return cast(expression.as_string(), String)


def _json_marker_is_active(column: Any, path: Sequence[str]) -> Any:
    """Mirror ``value not in (None, '', False)`` for a JSON path in SQL.

    JSON type is read before numeric/boolean casts.  This avoids PostgreSQL
    errors for arbitrary marker strings and keeps string ``"0"`` distinct from
    numeric zero.  Arrays and objects are active even when empty, matching the
    Python reducer's membership check.
    """

    value_type = _JsonPathType(column, path)
    value = _json_path_value(column, path)
    return case(
        (value_type.in_(("array", "object")), true()),
        (value_type.in_(("string", "text")), func.length(value) > 0),
        (
            value_type.in_(("number", "integer", "real")),
            cast(value, Numeric) != 0,
        ),
        (value_type == "true", true()),
        (value_type == "boolean", cast(value, Boolean)),
        else_=false(),
    )


def _json_value_is_truthy(column: Any, path: Sequence[str]) -> Any:
    """Mirror Python ``bool(metadata[path])`` for a JSON path in SQL."""

    value_type = _JsonPathType(column, path)
    value = _json_path_value(column, path)
    return case(
        (value_type == "true", true()),
        (value_type == "boolean", cast(value, Boolean)),
        (
            value_type.in_(("number", "integer", "real")),
            cast(value, Numeric) != 0,
        ),
        (value_type.in_(("string", "text")), func.length(value) > 0),
        (value_type == "array", and_(value.is_not(None), value != "[]")),
        (value_type == "object", and_(value.is_not(None), value != "{}")),
        else_=false(),
    )


def _canonical_uuid_text(value: Any) -> Any:
    """Render a UUID column as a canonical lower-case 36-char string.

    PostgreSQL's UUID cast already includes hyphens, while SQLite stores the
    SQLAlchemy UUID type as 32 compact hex characters.  Formatting the trusted
    database value lets the untrusted JSON reference remain an exact textual
    comparison and therefore preserves the pure reducer's validity rules.
    """

    compact = func.lower(func.replace(cast(value, String), literal("-"), literal("")))
    pieces = (
        (1, 8),
        (9, 4),
        (13, 4),
        (17, 4),
        (21, 12),
    )
    result: Any = None
    for index, length in pieces:
        part = cast(func.substr(compact, index, length), String)
        result = part if result is None else result + literal("-") + part
    return result


def _session_dialect_name(session: Any) -> str | None:
    """Read the SQLAlchemy dialect through a session or a thin test wrapper."""

    current = session
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        get_bind = getattr(current, "get_bind", None)
        bind = get_bind() if callable(get_bind) else getattr(current, "bind", None)
        dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
        if dialect_name:
            return str(dialect_name)
        current = getattr(current, "delegate", None)
    return None


def _id(value: Any) -> str | None:
    """Normalize UUID-like values without allowing an invalid value to leak."""

    if value in (None, ""):
        return None
    return str(value)


def _dt(value: Any) -> datetime | None:
    """Normalize database/JSON timestamps for deterministic comparisons."""

    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _row_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a SQLAlchemy row or a light-weight test object."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except AttributeError:
        pass
    # ``select(column, ...)`` rows expose a mapping view in SQLAlchemy 2.
    try:
        return value._mapping.get(name, default)
    except AttributeError:
        return default


def _created_key(value: Any) -> tuple[datetime, str]:
    created = _dt(_row_value(value, "created_at")) or datetime.min
    return created, _id(_row_value(value, "id")) or ""


_MISSING = object()


def _assistant_is_completion(
    metadata: Mapping[str, Any],
    *,
    message: Any | None = None,
) -> bool:
    """Return whether an assistant row represents a completed reply.

    Dialogue question rows are assistant messages in the transcript, but an
    internal ``agent_question`` is not a human-facing completion.  The
    metadata markers are server-issued and are intentionally checked here so
    the status projection cannot turn an internal question into ``ready``.
    """

    if message is not None:
        sender_type = str(_row_value(message, "sender_type", "") or "").casefold()
        if sender_type in {"system", "tool", "control"}:
            return False
        stored_content_length = _row_value(message, "stored_content_length", _MISSING)
        if stored_content_length is not _MISSING:
            try:
                if int(stored_content_length or 0) <= 0:
                    return False
            except (TypeError, ValueError):
                pass
        content = _row_value(message, "content", _MISSING)
        if content is not _MISSING and not str(content or "").strip():
            return False
    if any(
        metadata.get(key) not in (None, "", False)
        for key in (
            "agent_dialogue_input_id",
            "dialogue_input_id",
            "input_id",
            "agent_question",
            "human_question",
            "root_control_turn",
            "root_agent_control_relay",
            "agent_dialogue_control_relay",
            "agent_control_actor",
            "source_human_control",
            "budget_blocked",
            "restart_recovery_state",
            "safe_error_code",
        )
    ):
        return False
    dialogue_metadata = metadata.get("agent_dialogue")
    if isinstance(dialogue_metadata, Mapping) and dialogue_metadata.get("failure"):
        return False
    if metadata.get("input_type") not in (None, "", False):
        return False
    generation_state = str(metadata.get("generation_status") or "").casefold()
    if generation_state in _IGNORED_ASSISTANT_GENERATION_STATES:
        return False
    if bool(metadata.get("partial")):
        return False
    return True


def _latest_completion_at(
    *,
    runs: Sequence[Any],
    assistant_messages: Sequence[Any],
) -> datetime | None:
    """Return the newest server completion fence represented by the rows."""

    latest: datetime | None = None
    runs_by_id = {
        run_id: run
        for run in runs
        if (run_id := _id(_row_value(run, "id"))) is not None
    }
    for message in assistant_messages:
        metadata = _row_value(message, "message_metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        if not _assistant_is_completion(metadata, message=message):
            continue
        run_id = _id(metadata.get("agent_run_id"))
        run = runs_by_id.get(run_id) if run_id else None
        if run_id:
            if run is None or str(_row_value(run, "status", "") or "").casefold() != "succeeded":
                continue
            message_session_id = _id(_row_value(message, "session_id"))
            run_session_id = _id(_row_value(run, "session_id"))
            if message_session_id != run_session_id:
                continue
        message_at = _dt(_row_value(message, "created_at"))
        if message_at is None:
            continue
        # ``save_assistant_message`` persists the final sanitized reply before
        # the emitter/Run terminal callback.  The row timestamp is therefore
        # the user-visible completion fence.  Using ``Run.ended_at`` here
        # would reopen a reply that the user read in the small gap before the
        # terminal callback commits.
        completion_at = message_at
        if latest is None or completion_at > latest:
            latest = completion_at
    return latest


def project_conversation_status_projection(
    *,
    last_read_at: Any = None,
    runs: Sequence[Any] = (),
    assistant_messages: Sequence[Any] = (),
    human_inputs: Sequence[Any] = (),
) -> ConversationStatusProjection:
    """Project one session into the four shared history filter states.

    ``runs`` contains the Run rows for this session.  Run creation order is
    used for failure supersession: a newer successful Run clears an older
    failed Run, even if the older row receives a late update.  ``human_inputs``
    contains only the latest input revision for each dialogue after the batch
    loader has collapsed revisions.
    """

    normalized_runs = list(runs)
    normalized_inputs = tuple(_collapse_latest_inputs(human_inputs).values())
    completion_at = _latest_completion_at(
        runs=normalized_runs,
        assistant_messages=assistant_messages,
    )
    has_human_attention = any(
        str(_row_value(item, "state", "") or "").casefold() == "pending"
        and str(_row_value(item, "input_kind", "") or "").casefold() == "human_question"
        and _row_value(item, "answer_message_id") in (None, "")
        and str(_row_value(item, "dialogue_state", "") or "").casefold()
        not in _TERMINAL_DIALOGUE_STATES_EXCLUDING_PENDING
        for item in normalized_inputs
    )
    if has_human_attention:
        return ConversationStatusProjection("needs_attention", completion_at)

    # A failed attempt remains actionable while a retry is active.  A newer
    # successful attempt supersedes all older failures by immutable creation
    # order; late ``updated_at`` writes on an old failure cannot resurrect it.
    failures = [
        candidate
        for candidate in normalized_runs
        if str(_row_value(candidate, "status", "") or "").casefold() in _FAILURE_RUN_STATES
    ]
    successes = [
        candidate
        for candidate in normalized_runs
        if str(_row_value(candidate, "status", "") or "").casefold() == "succeeded"
    ]
    latest_failure = max(failures, key=_created_key, default=None)
    latest_success = max(successes, key=_created_key, default=None)
    if latest_failure is not None and (
        latest_success is None or _created_key(latest_failure) > _created_key(latest_success)
    ):
        return ConversationStatusProjection("needs_attention", completion_at)

    if any(
        str(_row_value(run, "status", "") or "").casefold() in _ACTIVE_RUN_STATES
        for run in normalized_runs
    ):
        return ConversationStatusProjection("working", completion_at)

    read_at = _dt(last_read_at)
    if completion_at is not None and (read_at is None or completion_at > read_at):
        return ConversationStatusProjection("ready", completion_at)

    return ConversationStatusProjection("idle", completion_at)


def project_conversation_status(
    *,
    last_read_at: Any = None,
    runs: Sequence[Any] = (),
    assistant_messages: Sequence[Any] = (),
    human_inputs: Sequence[Any] = (),
) -> ConversationStatus:
    """Return only the four-state value for callers that do not need a fence."""

    return project_conversation_status_projection(
        last_read_at=last_read_at,
        runs=runs,
        assistant_messages=assistant_messages,
        human_inputs=human_inputs,
    ).status


def _collapse_latest_inputs(rows: Sequence[Any]) -> dict[str, Any]:
    """Keep the latest input revision so answered/cancelled rows clear stale pending rows."""

    latest: dict[str, Any] = {}
    for row in rows:
        dialogue_id = _id(_row_value(row, "dialogue_id"))
        if dialogue_id is None:
            continue
        previous = latest.get(dialogue_id)
        revision = int(_row_value(row, "revision", 0) or 0)
        previous_revision = int(_row_value(previous, "revision", 0) or 0) if previous is not None else -1
        if previous is None or revision > previous_revision or (
            revision == previous_revision and _created_key(row) > _created_key(previous)
        ):
            latest[dialogue_id] = row
    return latest


async def project_conversation_statuses(
    session_ids: Sequence[Any],
    *,
    last_read_at_by_session: Mapping[str, Any] | None = None,
    db_session: Any | None = None,
) -> dict[str, ConversationStatusProjection]:
    """Load and project visible sessions with a constant number of queries.

    The three source queries are scoped to the caller-provided visible IDs.
    This function intentionally has no ACL logic and must only be called after
    the route has applied its existing session/project permissions.
    """

    normalized_ids: list[uuid.UUID] = []
    normalized_text_ids: list[str] = []
    for raw in session_ids:
        try:
            value = uuid.UUID(str(raw))
        except (TypeError, ValueError, AttributeError):
            continue
        if value not in normalized_ids:
            normalized_ids.append(value)
            normalized_text_ids.append(str(value))
    if not normalized_ids:
        return {}

    own_session = db_session is None
    db = db_session
    if own_session:
        db = await get_database_manager().get_session()

    runs: list[Any] = []
    messages: list[Any] = []
    input_rows: list[Any] = []
    try:
        # A fixed batch is deliberately used rather than a query per session.
        # Columns are selected explicitly so encrypted message bodies never
        # enter this projection.
        async def fetch_rows(statement: Any) -> list[Any]:
            result = await db.execute(statement)
            mappings = getattr(result, "mappings", None)
            if callable(mappings):
                return list(mappings().all())
            return list(result.all())

        run_category = case(
            (AgentRun.status.in_(_ACTIVE_RUN_STATES), literal("active")),
            (AgentRun.status.in_(_FAILURE_RUN_STATES), literal("failure")),
            (AgentRun.status == "succeeded", literal("success")),
            else_=literal("other"),
        ).label("status_category")
        run_rank = func.row_number().over(
            partition_by=(AgentRun.session_id, run_category),
            order_by=(desc(AgentRun.created_at), desc(AgentRun.id)),
        ).label("status_rank")
        run_source = (
            select(
                AgentRun.id,
                AgentRun.session_id,
                AgentRun.status,
                AgentRun.created_at,
                AgentRun.updated_at,
                AgentRun.ended_at,
                run_category,
                run_rank,
            )
            .where(
                AgentRun.session_id.in_(normalized_ids),
                AgentRun.status.in_(
                    tuple(_ACTIVE_RUN_STATES | _FAILURE_RUN_STATES | {"succeeded"})
                ),
            )
            .subquery()
        )
        runs = await fetch_rows(
            select(run_source).where(run_source.c.status_rank == 1)
        )

        # Keep the indexed, non-JSON candidate set small before converting
        # PostgreSQL's text-backed JSON column.  PostgreSQL materializes this
        # CTE so every marker/path probe below reuses the same JSONB value.
        # Other dialects retain their original JSON expressions and behavior.
        postgres_message_json = _session_dialect_name(db) == "postgresql"
        stored_message_metadata = (
            cast(ConversationMessage.message_metadata, JSONB)
            if postgres_message_json
            else ConversationMessage.message_metadata
        )
        sender_allowed = or_(
            ConversationMessage.sender_type.is_(None),
            ~func.lower(ConversationMessage.sender_type).in_(
                ("system", "tool", "control")
            ),
        )
        message_candidates = (
            select(
                ConversationMessage.id.label("id"),
                ConversationMessage.session_id.label("session_id"),
                ConversationMessage.created_at.label("created_at"),
                ConversationMessage.updated_at.label("updated_at"),
                stored_message_metadata.label("message_metadata"),
                ConversationMessage.sender_type.label("sender_type"),
                func.length(ConversationMessage._content).label(
                    "stored_content_length"
                ),
            )
            .where(
                ConversationMessage.session_id.in_(normalized_ids),
                ConversationMessage.role == "assistant",
                ConversationMessage.deleted_at.is_(None),
                sender_allowed,
                or_(
                    ConversationMessage.is_active_branch.is_(True),
                    ConversationMessage.is_active_branch.is_(None),
                ),
                func.length(ConversationMessage._content) > 0,
            )
            .cte("conversation_status_message_candidates")
        )
        if postgres_message_json:
            message_candidates = message_candidates.prefix_with(
                "MATERIALIZED", dialect="postgresql"
            )

        # Apply the complete completion predicate before ranking.  A fixed
        # row cap would let a run of control messages hide an older valid
        # completion, so the database chooses the latest *valid* row per
        # session.  The outer run join also enforces the referenced-run status
        # and same-session gate without casting untrusted JSON into UUID.
        message_metadata = message_candidates.c.message_metadata
        message_run_id = _json_path_value(message_metadata, ("agent_run_id",))
        message_run_match = and_(
            message_candidates.c.session_id == AgentRun.session_id,
            AgentRun.session_id.in_(normalized_ids),
            func.lower(AgentRun.status) == "succeeded",
            message_run_id == _canonical_uuid_text(AgentRun.id),
        )
        run_gate = or_(
            message_run_id.is_(None),
            message_run_id == literal(""),
            and_(
                func.lower(AgentRun.status) == "succeeded",
                AgentRun.session_id == message_candidates.c.session_id,
            ),
        )
        generation_state = _json_path_value(
            message_metadata, ("generation_status",)
        )
        generation_allowed = or_(
            generation_state.is_(None),
            ~func.lower(generation_state).in_(_IGNORED_ASSISTANT_GENERATION_STATES),
        )
        completion_allowed = and_(
            *(
                ~_json_marker_is_active(message_metadata, (marker,))
                for marker in _ASSISTANT_COMPLETION_MARKERS
            ),
            ~_json_value_is_truthy(message_metadata, ("agent_dialogue", "failure")),
            ~_json_marker_is_active(message_metadata, ("input_type",)),
            ~_json_value_is_truthy(message_metadata, ("partial",)),
        )
        message_rank = func.row_number().over(
            partition_by=message_candidates.c.session_id,
            order_by=(
                desc(message_candidates.c.created_at).nulls_last(),
                desc(message_candidates.c.id).nulls_last(),
            ),
        ).label("status_rank")
        message_source = (
            select(
                message_candidates.c.id,
                message_candidates.c.session_id,
                message_candidates.c.created_at,
                message_candidates.c.updated_at,
                message_candidates.c.message_metadata,
                message_candidates.c.sender_type,
                message_candidates.c.stored_content_length,
                AgentRun.id.label("referenced_run_id"),
                AgentRun.session_id.label("referenced_run_session_id"),
                AgentRun.status.label("referenced_run_status"),
                AgentRun.created_at.label("referenced_run_created_at"),
                AgentRun.updated_at.label("referenced_run_updated_at"),
                AgentRun.ended_at.label("referenced_run_ended_at"),
                message_rank,
            )
            .select_from(message_candidates)
            .outerjoin(AgentRun, message_run_match)
            .where(completion_allowed, generation_allowed, run_gate)
            .subquery()
        )
        messages = await fetch_rows(
            select(message_source).where(message_source.c.status_rank == 1)
        )
        for message in messages:
            referenced_run_id = _row_value(message, "referenced_run_id")
            if referenced_run_id is None:
                continue
            # The message query already proved same-session/succeeded.  Feed
            # that bounded join result into the reducer so an older valid
            # completion remains visible even when Run summary ranking keeps
            # only a newer successful attempt.
            runs.append(
                {
                    "id": referenced_run_id,
                    "session_id": _row_value(
                        message, "referenced_run_session_id"
                    ),
                    "status": _row_value(message, "referenced_run_status"),
                    "created_at": _row_value(
                        message, "referenced_run_created_at"
                    ),
                    "updated_at": _row_value(
                        message, "referenced_run_updated_at"
                    ),
                    "ended_at": _row_value(message, "referenced_run_ended_at"),
                }
            )

        input_rank = func.row_number().over(
            partition_by=AgentDialogueInput.dialogue_id,
            order_by=(
                desc(AgentDialogueInput.revision).nulls_last(),
                desc(AgentDialogueInput.created_at).nulls_last(),
                desc(AgentDialogueInput.id).nulls_last(),
            ),
        ).label("status_rank")
        input_latest = (
            select(
                AgentDialogueInput.id,
                AgentDialogueInput.dialogue_id,
                AgentDialogueInput.input_kind,
                AgentDialogueInput.state,
                AgentDialogueInput.answer_message_id,
                AgentDialogueInput.source_run_id,
                AgentDialogueInput.revision,
                AgentDialogueInput.created_at,
                AgentDialogue.root_session_id,
                AgentDialogue.worker_session_id,
                AgentRun.session_id.label("source_session_id"),
                AgentDialogue.state.label("dialogue_state"),
                input_rank,
            )
            .join(
                AgentDialogue,
                AgentDialogue.id == AgentDialogueInput.dialogue_id,
            )
            .outerjoin(
                AgentRun,
                AgentRun.id == AgentDialogueInput.source_run_id,
            )
            .where(
                or_(
                    AgentDialogue.root_session_id.in_(normalized_ids),
                    AgentDialogue.worker_session_id.in_(normalized_ids),
                )
            )
            .subquery()
        )
        input_latest = select(input_latest).where(
            input_latest.c.status_rank == 1
        ).cte("latest_dialogue_inputs")
        answer_message_id = cast(input_latest.c.answer_message_id, String)
        input_allowed = and_(
            func.lower(input_latest.c.input_kind) == "human_question",
            func.lower(input_latest.c.state) == "pending",
            or_(
                input_latest.c.answer_message_id.is_(None),
                answer_message_id == literal(""),
            ),
            or_(
                input_latest.c.dialogue_state.is_(None),
                ~func.lower(input_latest.c.dialogue_state).in_(
                    _TERMINAL_DIALOGUE_STATES_EXCLUDING_PENDING
                ),
            ),
        )
        input_columns = [
            input_latest.c[column_name]
            for column_name in (
                "id",
                "dialogue_id",
                "input_kind",
                "state",
                "answer_message_id",
                "source_run_id",
                "source_session_id",
                "revision",
                "created_at",
                "root_session_id",
                "worker_session_id",
                "dialogue_state",
                "status_rank",
            )
        ]
        root_inputs = select(
            *input_columns,
            input_latest.c.root_session_id.label("projected_session_id"),
        ).where(
            input_latest.c.root_session_id.in_(normalized_ids),
            input_allowed,
        )
        worker_inputs = select(
            *input_columns,
            input_latest.c.worker_session_id.label("projected_session_id"),
        ).where(
            input_latest.c.worker_session_id.in_(normalized_ids),
            input_latest.c.source_session_id == input_latest.c.worker_session_id,
            input_allowed,
        )
        visible_inputs = union_all(root_inputs, worker_inputs).subquery()
        visible_input_rank = func.row_number().over(
            partition_by=visible_inputs.c.projected_session_id,
            order_by=(
                desc(visible_inputs.c.revision).nulls_last(),
                desc(visible_inputs.c.created_at).nulls_last(),
                desc(visible_inputs.c.id).nulls_last(),
            ),
        ).label("session_rank")
        input_source = select(visible_inputs, visible_input_rank).subquery()
        input_rows = await fetch_rows(
            select(input_source).where(input_source.c.session_rank == 1)
        )
    finally:
        if own_session:
            await db.close()

    runs_by_session: dict[str, list[Any]] = defaultdict(list)
    for row in runs:
        session_id = _id(_row_value(row, "session_id"))
        if session_id is not None:
            runs_by_session[session_id].append(row)

    messages_by_session: dict[str, list[Any]] = defaultdict(list)
    for row in messages:
        session_id = _id(_row_value(row, "session_id"))
        if session_id is not None:
            messages_by_session[session_id].append(row)

    latest_inputs = _collapse_latest_inputs(input_rows)
    inputs_by_session: dict[str, list[Any]] = defaultdict(list)
    for row in latest_inputs.values():
        root_session_id = _id(_row_value(row, "root_session_id"))
        worker_session_id = _id(_row_value(row, "worker_session_id"))
        source_session_id = _id(_row_value(row, "source_session_id"))
        if root_session_id in normalized_text_ids:
            inputs_by_session[root_session_id].append(row)
        # A worker-side status is only projected from an input whose durable
        # source Run belongs to that worker session.  This prevents a
        # participant-visible child session from learning a pending input
        # attached solely to an inaccessible/private root.
        if (
            worker_session_id in normalized_text_ids
            and source_session_id == worker_session_id
        ):
            inputs_by_session[worker_session_id].append(row)

    read_map = last_read_at_by_session or {}
    return {
        session_id: project_conversation_status_projection(
            last_read_at=read_map.get(session_id),
            runs=runs_by_session.get(session_id, ()),
            assistant_messages=messages_by_session.get(session_id, ()),
            human_inputs=inputs_by_session.get(session_id, ()),
        )
        for session_id in normalized_text_ids
    }


__all__ = [
    "ConversationStatus",
    "ConversationStatusProjection",
    "project_conversation_status",
    "project_conversation_status_projection",
    "project_conversation_statuses",
]
