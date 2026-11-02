"""Exact opaque receipts for the mobile conversation-message projection.

The general table's public page digest cannot skip a multi-page message table.
This additive protocol hashes every stored column in PostgreSQL, then hashes
the ordered ``id:fingerprint`` set.  The mobile client stores each per-row
fingerprint beside its cached message and can therefore check the same set
without loading message bodies into JavaScript.

The five-minute overlap is only a bounded replay window.  A client accepts a
delta only after its local set digest matches the server's terminal digest;
otherwise it performs the existing full paginated pull.  Old mobile clients do
not opt in and retain the existing cursor behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from ..memory.models import ConversationMessage, ConversationSession
from ..memory.conversation_sync_receipts import (
    conversation_message_sync_fingerprint_expression,
)


GENERAL_MESSAGE_SYNC_VERSION = 1
GENERAL_MESSAGE_DELTA_OVERLAP = timedelta(minutes=5)
GENERAL_MESSAGE_FINGERPRINT_FIELD = "_sync_fingerprint"


def conversation_message_delta_lower_bound(server_watermark: datetime) -> datetime:
    """Replay the five-minute overlap, including an exact-boundary timestamp."""

    return (
        server_watermark
        - GENERAL_MESSAGE_DELTA_OVERLAP
        - timedelta(microseconds=1)
    )


def conversation_message_sync_digest_statement(
    *,
    visibility: Any,
    event_expression: Any,
    snapshot_at: Any,
    include_authoritative_ids: bool = False,
) -> Any:
    """Build one PostgreSQL aggregate over active visible messages in a snapshot."""

    row_fingerprint = conversation_message_sync_fingerprint_expression()
    row_material = func.concat(
        cast(ConversationMessage.id, String()),
        literal(":"),
        row_fingerprint,
    )
    ordered_delimiter = aggregate_order_by(literal("\n"), ConversationMessage.id)
    digest = func.md5(
        func.coalesce(
            func.string_agg(row_material, ordered_delimiter),
            literal(""),
        )
    )
    columns = [
        func.count(ConversationMessage.id).label("row_count"),
        digest.label("digest"),
    ]
    if include_authoritative_ids:
        columns.append(
            func.array_agg(
                aggregate_order_by(
                    ConversationMessage.id,
                    ConversationMessage.id,
                )
            ).label("authoritative_ids")
        )
    return (
        select(*columns)
        .select_from(ConversationMessage)
        .join(
            ConversationSession,
            ConversationMessage.session_id == ConversationSession.id,
        )
        .where(
            visibility,
            ConversationSession.deleted_at.is_(None),
            ConversationMessage.deleted_at.is_(None),
            event_expression <= snapshot_at,
        )
    )
