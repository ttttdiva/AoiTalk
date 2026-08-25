"""Rename task status done to closed.

Revision ID: 20260422_0008
Revises: 20260421_0007
Create Date: 2026-04-22 16:30:00
"""

from __future__ import annotations

from alembic import op

revision = "20260422_0008"
down_revision = "20260421_0007"
branch_labels = None
depends_on = None


def _update_recurrence_status_if_column_exists(
    column_name: str, from_status: str, to_status: str
) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'task_recurrence_rules'
                  AND column_name = '{column_name}'
            ) THEN
                UPDATE task_recurrence_rules
                SET {column_name} = '{to_status}'
                WHERE {column_name} = '{from_status}';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    op.execute("UPDATE tasks SET status = 'closed' WHERE status = 'done'")
    op.execute("UPDATE task_occurrences SET status = 'closed' WHERE status = 'done'")
    _update_recurrence_status_if_column_exists("trigger_status", "done", "closed")
    _update_recurrence_status_if_column_exists("reset_status_to", "done", "closed")


def downgrade() -> None:
    op.execute("UPDATE tasks SET status = 'done' WHERE status = 'closed'")
    op.execute("UPDATE task_occurrences SET status = 'done' WHERE status = 'closed'")
    _update_recurrence_status_if_column_exists("trigger_status", "closed", "done")
    _update_recurrence_status_if_column_exists("reset_status_to", "closed", "done")
