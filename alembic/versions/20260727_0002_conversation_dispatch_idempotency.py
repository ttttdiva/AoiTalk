"""Add an atomic idempotency key for conversation dispatch.

Revision ID: 20260727_0002
Revises: 20260727_0001
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "20260727_0002"
down_revision = "20260727_0001"
branch_labels = None
depends_on = None


def _client_message_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("client_message_id", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("client_message_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    # 既存metadataから各keyの最初のrunだけを安全に引き継ぐ。
    bind = op.get_bind()
    existing_dispatches = bind.execute(
        sa.text(
            """
            SELECT id,
                   session_id,
                   user_id,
                   run_metadata->>'client_message_id' AS client_message_id
            FROM agent_runs
            WHERE session_id IS NOT NULL
              AND run_metadata->>'client_message_id' IS NOT NULL
            ORDER BY created_at, id
            """
        )
    )
    update_dispatch = sa.text(
        """
        UPDATE agent_runs
        SET client_message_id = :client_message_id,
            client_message_key = :client_message_key
        WHERE id = :run_id
        """
    )
    seen_dispatches: set[tuple[str, str, str]] = set()
    for row in existing_dispatches:
        raw_client_message_id = str(row.client_message_id).strip()
        dispatch_identity = (
            str(row.session_id),
            str(getattr(row, "user_id", None) or ""),
            raw_client_message_id,
        )
        if (
            not raw_client_message_id
            or len(raw_client_message_id) > 512
            or dispatch_identity in seen_dispatches
        ):
            continue
        seen_dispatches.add(dispatch_identity)
        bind.execute(
            update_dispatch,
            {
                "run_id": row.id,
                "client_message_id": raw_client_message_id,
                "client_message_key": _client_message_key(raw_client_message_id),
            },
        )
    op.create_unique_constraint(
        "uq_agent_runs_session_user_client_message_key",
        "agent_runs",
        ["session_id", "user_id", "client_message_key"],
    )
    op.create_table(
        "conversation_dispatch_outbox",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("client_message_id", sa.String(length=512), nullable=False),
        sa.Column("client_message_key", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "session_id",
            "user_id",
            "client_message_key",
            name="uq_conversation_dispatch_outbox_session_user_client_key",
        ),
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_session_id",
        "conversation_dispatch_outbox",
        ["session_id"],
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_status",
        "conversation_dispatch_outbox",
        ["status"],
    )
    op.create_index(
        "ix_conversation_dispatch_outbox_lease_expires_at",
        "conversation_dispatch_outbox",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_dispatch_outbox_lease_expires_at",
        table_name="conversation_dispatch_outbox",
    )
    op.drop_index(
        "ix_conversation_dispatch_outbox_status",
        table_name="conversation_dispatch_outbox",
    )
    op.drop_index(
        "ix_conversation_dispatch_outbox_session_id",
        table_name="conversation_dispatch_outbox",
    )
    op.drop_table("conversation_dispatch_outbox")
    op.drop_constraint(
        "uq_agent_runs_session_user_client_message_key",
        "agent_runs",
        type_="unique",
    )
    op.drop_column("agent_runs", "request_fingerprint")
    op.drop_column("agent_runs", "client_message_key")
    op.drop_column("agent_runs", "client_message_id")
