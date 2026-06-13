"""notification_deliveries.task_id に ondelete='SET NULL' を追加

Revision ID: 20260320_0004
Revises: 20260320_0003
Create Date: 2026-03-20 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260320_0004"
down_revision = "20260320_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "notification_deliveries_task_id_fkey",
        "notification_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "notification_deliveries_task_id_fkey",
        "notification_deliveries",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "notification_deliveries_task_id_fkey",
        "notification_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "notification_deliveries_task_id_fkey",
        "notification_deliveries",
        "tasks",
        ["task_id"],
        ["id"],
    )
