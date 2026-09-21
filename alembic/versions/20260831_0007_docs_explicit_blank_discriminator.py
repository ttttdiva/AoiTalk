"""Add the encrypted Docs explicit-blank discriminator.

Revision ID: 20260831_0007
Revises: 20260831_0006

``knowledge_nodes.body_json`` is encrypted for application-managed rows, so
visibility SQL cannot safely inspect the persisted ``blank`` marker.  Keep a
small non-sensitive discriminator in its own column.  The upgrade performs a
crypto-aware backfill while the application key is available; a separate
idempotent verifier remains available for rolling deployments.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


def _strict_explicit_blank(title: object, body_json: object, node_type: object, system_key: object) -> bool:
    return (
        title == ""
        and str(node_type or "") == "node"
        and not system_key
        and isinstance(body_json, dict)
        and body_json.get("format") == "doc_block"
        and body_json.get("block_type") == "paragraph"
        and body_json.get("blank") is True
    )


def _backfill_explicit_blank(bind: sa.Connection) -> None:
    """Derive the projection from decrypted body_json without fail-open SQL."""

    # Import lazily: importing the crypto provider at module import time would
    # make ``alembic heads`` depend on a configured key even when no upgrade
    # runs.  During upgrade the key is mandatory; a decrypt failure aborts the
    # migration rather than classifying an encrypted row as legacy.
    from src.security.field_crypto import (
        decrypt_json_value_if_needed,
        decrypt_text_if_needed,
    )

    rows = bind.execute(
        sa.text(
            """
            select id, title, body_json, body_text, node_type, system_key, is_explicit_blank
            from knowledge_nodes
            where title = '' or is_explicit_blank = true
            order by id
            """
        )
    ).mappings()
    updates: list[dict[str, object]] = []
    for row in rows:
        try:
            body_json = decrypt_json_value_if_needed(
                row["body_json"], aad="knowledge_nodes.body_json"
            )
        except Exception as exc:  # pragma: no cover - depends on deployment key
            raise RuntimeError(
                "knowledge_nodes explicit-blank backfill could not decrypt body_json"
            ) from exc
        try:
            # ``body_text`` is a title mirror and is part of the blank
            # invariant.  Validate/decrypt it before replacing a stale mirror
            # with the canonical empty value; a tampered ciphertext must abort
            # the migration rather than be silently discarded.
            decrypt_text_if_needed(row["body_text"], aad="knowledge_nodes.body_text")
        except Exception as exc:  # pragma: no cover - depends on deployment key
            raise RuntimeError(
                "knowledge_nodes explicit-blank backfill could not decrypt body_text"
            ) from exc
        expected = _strict_explicit_blank(
            row["title"], body_json, row["node_type"], row["system_key"]
        )
        if bool(row["is_explicit_blank"]) != expected or (
            expected and row["body_text"] not in (None, "")
        ):
            updates.append({
                "id": row["id"],
                "value": expected,
                "body_text": "" if expected else row["body_text"],
            })
    if updates:
        bind.execute(
            sa.text(
                """
                update knowledge_nodes
                set is_explicit_blank = :value,
                    body_text = :body_text
                where id = :id
                """
            ),
            updates,
        )


revision = "20260831_0007"
down_revision = "20260831_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_nodes")}
    if "is_explicit_blank" not in columns:
        # A server default makes the addition safe for existing rows and for
        # old writers during a rolling deploy.  The default may be dropped by
        # a later schema-hardening migration once every writer dual-writes it.
        op.add_column(
            "knowledge_nodes",
            sa.Column(
                "is_explicit_blank",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    indexes = {index["name"] for index in inspector.get_indexes("knowledge_nodes")}
    if "ix_knowledge_nodes_is_explicit_blank" not in indexes:
        op.create_index(
            "ix_knowledge_nodes_is_explicit_blank",
            "knowledge_nodes",
            ["is_explicit_blank"],
        )
    _backfill_explicit_blank(op.get_bind())


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("knowledge_nodes")}
    if "ix_knowledge_nodes_is_explicit_blank" in indexes:
        op.drop_index("ix_knowledge_nodes_is_explicit_blank", table_name="knowledge_nodes")
    columns = {column["name"] for column in inspector.get_columns("knowledge_nodes")}
    if "is_explicit_blank" in columns:
        op.drop_column("knowledge_nodes", "is_explicit_blank")
