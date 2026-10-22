"""Persist the immutable App Action output schema for result verification."""

from alembic import op
import sqlalchemy as sa


revision = "20261018_0003"
down_revision = "20261018_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_action_runs",
        sa.Column("output_schema_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_action_runs", "output_schema_json")
