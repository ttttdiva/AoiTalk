"""Append canonical chat trigger facts without owning the caller's transaction."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timezone

from sqlalchemy import inspect, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import ConversationMessage, ConversationSession, Project
from ..memory.models.agent_automation import AgentAutomationEvent


_CAUSATION_IDS = (
    "causation_id",
    "root_work_item_id",
    "origin_work_item_id",
    "origin_agent_id",
    "origin_action_id",
    "automation_rule_id",
    "automation_rule_revision_id",
    "source_event_id",
    "fork_source_message_id",
)


def _uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def is_automation_generated(metadata) -> bool:
    """Causation can suppress a trigger, including malformed marker values."""
    if not isinstance(metadata, dict):
        return True
    return (
        "automation_generated" in metadata
        and metadata["automation_generated"] is not False
    ) or any(key in metadata for key in _CAUSATION_IDS) or (
        "causal_depth" in metadata
        and (type(metadata["causal_depth"]) is not int or metadata["causal_depth"] != 0)
    )


def _safe_metadata(message: ConversationMessage) -> dict:
    # Metadata can only suppress triggering, never confer human authority.
    # Even malformed causation IDs suppress; only UUIDs survive projection.
    metadata = message.message_metadata
    safe = {"automation_generated": is_automation_generated(metadata)}
    if not isinstance(metadata, dict):
        return safe
    for key in _CAUSATION_IDS:
        value = _uuid(metadata.get(key))
        if value is not None:
            safe[key] = str(value)
    if "causal_depth" in metadata:
        depth = metadata["causal_depth"]
        if type(depth) is int and 0 <= depth <= 100:
            safe["causal_depth"] = depth
    return safe


def _sender(message: ConversationMessage) -> tuple[str, uuid.UUID | None, str | None]:
    sender_type = message.sender_type
    if sender_type == "agent":
        return "agent", _uuid(message.sender_id), None
    if sender_type in {"assistant", "character", "service"}:
        return "service", None, f"chat.{sender_type}"
    if message.role == "assistant":
        return "service", None, "chat.assistant"
    if (
        message.role == "user"
        and sender_type in {"user", "human"}
        and isinstance(message.sender_id, str)
        and message.sender_id.strip()
    ):
        return "human", _uuid(message.sender_id), None
    # Role alone, session ownership, display names and JSON are not principals.
    return "system", None, None


def chat_message_event_values(
    message: ConversationMessage,
    conversation: ConversationSession,
    *,
    space_id: uuid.UUID | None = None,
) -> dict:
    """Compute expected event values without I/O, decryption or ORM mutation.

    The message must have been flushed. Resolve ``space_id`` from the canonical
    Project, not JSON; ConversationSession has no space column. Consumers must
    separately reject deleted/inactive sources and compare these expected
    fields (including event_hash) with the immutable fact. Content edits and
    unrelated metadata do not affect the hash; sender, scope and causation do.
    """
    if conversation is None or message.session_id != conversation.id:
        raise ValueError("automation_conversation_mismatch")
    if message.id is None:
        raise ValueError("automation_message_identity_missing")
    occurred_at = message.created_at
    if occurred_at is None:
        raise ValueError("automation_message_timestamp_missing")
    if occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    revision = _digest({
        "message_id": str(message.id),
        "session_id": str(message.session_id),
        "created_at": occurred_at.isoformat(timespec="microseconds"),
    })
    identity = {
        "event_type": "chat.message.created",
        "source_type": "conversation_message",
        "source_id": str(message.id),
        "source_revision": revision,
    }
    event_id = uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + _canonical_json(identity))
    actor_kind, actor_id, service_actor_key = _sender(message)
    values = dict(
        id=event_id,
        **identity,
        project_id=conversation.project_id,
        space_id=space_id,
        conversation_session_id=conversation.id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        service_actor_key=service_actor_key,
        safe_metadata_json=_safe_metadata(message),
        occurred_at=occurred_at,
    )
    # Hash opaque sender identity too: legacy non-UUID senders have no actor_id,
    # but changing one such sender to another must invalidate the old fact.
    values["event_hash"] = _digest({
        "event": {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in {**values, "occurred_at": occurred_at.isoformat(timespec="microseconds")}.items()
        },
        "sender_identity_hash": _digest({
            "role": message.role,
            "sender_type": message.sender_type,
            "sender_id": message.sender_id,
        }),
    })
    return values


async def record_chat_message_event(
    session: AsyncSession,
    message: ConversationMessage,
    conversation: ConversationSession,
) -> AgentAutomationEvent:
    """Insert/replay in the caller's transaction; never commit or update facts.

    Flush assigns the message's durable ID and timestamp. Errors escape so the
    owner rolls back the message and event together. There is no queue state,
    provider call, or after-commit best-effort delivery here.
    """
    if inspect(message).session is not session.sync_session:
        raise ValueError("automation_message_session_mismatch")
    if conversation is None or message.session_id != conversation.id:
        raise ValueError("automation_conversation_mismatch")
    await session.flush()
    space_id = None
    if conversation.project_id is not None:
        space_id = await session.scalar(
            select(Project.space_id).where(Project.id == conversation.project_id)
        )
    values = chat_message_event_values(message, conversation, space_id=space_id)
    identity = {key: values[key] for key in (
        "event_type", "source_type", "source_id", "source_revision",
    )}
    dialect = session.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise ValueError("automation_event_dialect_unsupported")
    insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
    # Never UPDATE an existing fact, including a replay with changed metadata.
    statement = insert(AgentAutomationEvent).values(**values).on_conflict_do_nothing(
        index_elements=list(identity)
    )
    await session.execute(statement)
    return (await session.scalars(
        select(AgentAutomationEvent).filter_by(**identity)
    )).one()
