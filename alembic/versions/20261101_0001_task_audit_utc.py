"""Use UTC for new task audit timestamps, without rewriting existing rows.

Revision ID: 20261101_0001
Revises: 20261030_0003
"""

from alembic import op
import sqlalchemy as sa

revision = "20261101_0001"
down_revision = "20261030_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for column in ("created_at", "updated_at"):
            op.alter_column(
                "tasks",
                column,
                server_default=sa.text("timezone('UTC', CURRENT_TIMESTAMP)"),
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for column in ("created_at", "updated_at"):
            op.alter_column(
                "tasks", column, server_default=sa.text("CURRENT_TIMESTAMP")
            )
