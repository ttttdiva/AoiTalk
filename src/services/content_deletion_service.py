"""Shared helpers for content-deletion audit and retention policy.

This module deliberately keeps configuration parsing independent from the DB
runtime.  Importing it in a unit test therefore does not open a connection or
load the full memory model registry; the model is imported only when an event
is actually appended.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


DELETION_RETENTION_ENV = "AOITALK_DELETION_RETENTION_DAYS"
DEFAULT_DELETION_RETENTION_DAYS = 30
# Ten years is intentionally conservative: malformed or accidental values
# should fail safe to the short default rather than retain deletion metadata
# indefinitely.
MAX_DELETION_RETENTION_DAYS = 3650

DELETION_EVENT_ACTIONS = frozenset(
    {"deleted", "restored", "purged", "permanent_deleted"}
)


def parse_deletion_retention_days(value: Any = None) -> int:
    """Return a bounded deletion-event retention period in days.

    ``None`` reads :envvar:`AOITALK_DELETION_RETENTION_DAYS`.  Missing,
    non-integral, non-positive, or extreme values all use the safe 30-day
    default.  The parser accepts an integer or the string representation of
    one, but intentionally rejects booleans and floating point values.
    """

    raw = os.getenv(DELETION_RETENTION_ENV) if value is None else value
    if isinstance(raw, bool):
        return DEFAULT_DELETION_RETENTION_DAYS
    if isinstance(raw, int):
        days = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return DEFAULT_DELETION_RETENTION_DAYS
        # ``int`` accepts values such as ``+30`` but also accepts surrounding
        # whitespace (already stripped); reject decimal/scientific forms.
        try:
            if text[0] in "+-":
                digits = text[1:]
            else:
                digits = text
            if not digits.isascii() or not digits.isdigit():
                return DEFAULT_DELETION_RETENTION_DAYS
            days = int(text, 10)
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_DELETION_RETENTION_DAYS

    if days <= 0 or days > MAX_DELETION_RETENTION_DAYS:
        return DEFAULT_DELETION_RETENTION_DAYS
    return days


def get_deletion_retention_days(value: Any = None) -> int:
    """Compatibility/readability alias for :func:`parse_deletion_retention_days`."""

    return parse_deletion_retention_days(value)


def deletion_retention_days(value: Any = None) -> int:
    """Short alias used by cleanup callers."""

    return parse_deletion_retention_days(value)


def _coerce_uuid(value: Any, field_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _required_text(value: Any, field_name: str, max_length: int) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return text


def _optional_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # A display name is advisory metadata; clipping it must not make the
    # destructive operation fail merely because a user supplied a long label.
    return text[:max_length] or None


async def append_event(
    session: AsyncSession,
    entity_type: str,
    entity_id: Any,
    *,
    action: str,
    root_entity_id: Any = None,
    root_id: Any = None,
    batch_id: Any = None,
    project_id: Any = None,
    actor_user_id: Any = None,
    display_name: Any = None,
    source: str = "unknown",
    event_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Any:
    """Append one deletion lifecycle event to the caller's transaction.

    The helper never commits.  It flushes so callers can safely continue with
    a physical delete in the same transaction while still receiving the
    generated event ID.  Subject IDs are kept as opaque strings and no body or
    content field is accepted; ``metadata`` is reserved for small provenance
    values such as paths, operation IDs, and failure details.

    ``root_id`` is accepted as a compatibility spelling for
    ``root_entity_id``.  Supplying both with different values is rejected.
    """

    if root_entity_id is not None and root_id is not None:
        if str(root_entity_id) != str(root_id):
            raise ValueError("root_entity_id and root_id disagree")
    if root_entity_id is None:
        root_entity_id = root_id

    normalized_action = _required_text(action, "action", 32).lower()
    if normalized_action not in DELETION_EVENT_ACTIONS:
        raise ValueError(
            "action must be one of: " + ", ".join(sorted(DELETION_EVENT_ACTIONS))
        )
    normalized_metadata: dict[str, Any]
    if metadata is None:
        normalized_metadata = {}
    elif isinstance(metadata, Mapping):
        normalized_metadata = dict(metadata)
    else:
        raise ValueError("metadata must be a mapping or None")

    # The ledger is intentionally content-minimized.  Reject obvious body
    # fields rather than relying on every caller to remember the privacy
    # contract.  Nested metadata is rejected too; callers may keep counts,
    # IDs, paths, and operation names but never plaintext content.
    forbidden_tokens = (
        "body",
        "content",
        "description",
        "message",
        "bytes",
        "attachment",
    )

    def _contains_forbidden_key(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                key_text = str(key).strip().lower()
                if any(token in key_text for token in forbidden_tokens):
                    return True
                if _contains_forbidden_key(nested):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(_contains_forbidden_key(item) for item in value)
        return False

    if _contains_forbidden_key(normalized_metadata):
        raise ValueError("deletion audit metadata must not contain content fields")

    normalized_batch_id = _coerce_uuid(batch_id, "batch_id") or uuid.uuid4()

    # Importing the model lazily keeps parser-only imports independent of the
    # full SQLAlchemy model graph and optional runtime dependencies.
    from ..memory.models.audit import ContentDeletionEvent

    event = ContentDeletionEvent(
        entity_type=_required_text(entity_type, "entity_type", 32),
        entity_id=_required_text(entity_id, "entity_id", 512),
        root_entity_id=_optional_text(root_entity_id, 512),
        batch_id=normalized_batch_id,
        project_id=_coerce_uuid(project_id, "project_id"),
        actor_user_id=_coerce_uuid(actor_user_id, "actor_user_id"),
        action=normalized_action,
        display_name=_optional_text(display_name, 255),
        source=_optional_text(source, 64),
        event_at=event_at or datetime.utcnow(),
        event_metadata=normalized_metadata,
    )
    added = session.add(event)
    # SQLAlchemy's AsyncSession.add is synchronous, while lightweight tests
    # often use AsyncMock for the entire session.  Accommodate both without
    # leaking an un-awaited coroutine in the latter case.
    if hasattr(added, "__await__"):
        await added
    await session.flush()
    return event


# Explicit names make call sites self-documenting while retaining the compact
# ``append_event`` spelling for shared deletion code.
append_content_deletion_event = append_event
append_deletion_event = append_event


__all__ = [
    "DELETION_EVENT_ACTIONS",
    "DELETION_RETENTION_ENV",
    "DEFAULT_DELETION_RETENTION_DAYS",
    "MAX_DELETION_RETENTION_DAYS",
    "append_content_deletion_event",
    "append_deletion_event",
    "append_event",
    "deletion_retention_days",
    "get_deletion_retention_days",
    "parse_deletion_retention_days",
]
