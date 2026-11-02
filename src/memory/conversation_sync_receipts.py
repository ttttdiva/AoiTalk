"""Database expressions for opaque conversation-message sync fingerprints."""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, func

from .models import ConversationMessage


def conversation_message_sync_fingerprint_expression() -> Any:
    """Hash every persisted message column without loading content into Python.

    ``ConversationMessage.content`` is decrypted by an ORM descriptor. Refer to
    the mapped table columns so PostgreSQL hashes the stored representation and
    callers receive only the opaque fingerprint.
    """

    table = ConversationMessage.__table__
    return func.md5(cast(func.jsonb_build_array(*table.columns), String()))
