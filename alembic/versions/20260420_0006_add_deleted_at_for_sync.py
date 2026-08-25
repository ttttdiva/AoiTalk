"""Add deleted_at (tombstone) columns for mobile sync delta pull.

Targets: projects, tasks, task_occurrences, time_entries, conversation_messages.
Also adds `updated_at` to conversation_messages (previously missing).

Revision ID: 20260420_0006
Revises: 20260420_0005
Create Date: 2026-04-20 14:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260420_0006"
down_revision = "20260420_0005"
branch_labels = None
depends_on = None


_TOMBSTONE_TABLES = (
    "projects",
    "tasks",
    "task_occurrences",
    "time_entries",
    "conversation_messages",
)


def upgrade() -> None:
    # 1. Add deleted_at to each target table (idempotent guard via information_schema).
    for table in _TOMBSTONE_TABLES:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = '{table}'
                          AND column_name = 'deleted_at'
                    ) THEN
                        ALTER TABLE {table} ADD COLUMN deleted_at TIMESTAMP NULL;
                    END IF;
                END $$;
                """
            )
        )
        op.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at "
                f"ON {table} (deleted_at)"
            )
        )

    # 2. conversation_messages: also add updated_at if missing (existing rows use created_at).
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'conversation_messages'
                      AND column_name = 'updated_at'
                ) THEN
                    ALTER TABLE conversation_messages ADD COLUMN updated_at TIMESTAMP NULL;
                    UPDATE conversation_messages SET updated_at = created_at WHERE updated_at IS NULL;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    for table in _TOMBSTONE_TABLES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_deleted_at"))
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS deleted_at"))

    op.execute(
        sa.text(
            "ALTER TABLE conversation_messages DROP COLUMN IF EXISTS updated_at"
        )
    )
