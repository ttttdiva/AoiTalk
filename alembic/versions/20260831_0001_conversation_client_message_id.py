"""Add a durable idempotency identity to conversation messages.

The Web BFF and the FastAPI conversation writer both accept a stable
``client_message_id`` for retried user/assistant writes.  The nullable column
keeps legacy/system rows valid while a partial unique index makes a duplicate
within one conversation impossible at the database boundary.

Legacy rows that already mirror an id in ``message_metadata`` are backfilled.
If historical data contains duplicate ids, the earliest row remains the
authority and later rows are left intact with a NULL typed id; no transcript
content is deleted.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260831_0001"
down_revision = "20260830_0005"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_conversation_messages_session_client_message_id"


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("client_message_id", sa.String(length=512), nullable=True),
    )

    # Preserve the existing JSON metadata contract while promoting valid
    # values to a typed, indexed identity.  Invalid/oversized values remain
    # legacy metadata and are not made authoritative.
    op.execute(
        sa.text(
            """
            UPDATE conversation_messages
            SET client_message_id = NULLIF(
                BTRIM((message_metadata::jsonb ->> 'client_message_id')),
                ''
            )
            WHERE message_metadata IS NOT NULL
              AND message_metadata::jsonb ? 'client_message_id'
              AND length(BTRIM(message_metadata::jsonb ->> 'client_message_id'))
                  BETWEEN 1 AND 512
            """
        )
    )

    # Do not delete duplicate historical messages.  Keep the deterministic
    # earliest row as the typed idempotency authority and clear only later
    # typed identities before creating the unique index.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY session_id, client_message_id
                        ORDER BY created_at ASC, id ASC
                    ) AS row_number
                FROM conversation_messages
                WHERE client_message_id IS NOT NULL
            )
            UPDATE conversation_messages AS message
            SET client_message_id = NULL
            FROM ranked
            WHERE message.id = ranked.id
              AND ranked.row_number > 1
            """
        )
    )

    op.create_index(
        INDEX_NAME,
        "conversation_messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="conversation_messages")
    op.drop_column("conversation_messages", "client_message_id")
