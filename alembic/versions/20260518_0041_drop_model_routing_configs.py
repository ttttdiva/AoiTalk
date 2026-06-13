"""drop model routing configs

Revision ID: 20260518_0041
Revises: 20260518_0040
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260518_0041"
down_revision = "20260518_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("model_routing_configs")


def downgrade() -> None:
    op.create_table(
        "model_routing_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("strategy", sa.String(length=20), nullable=True),
        sa.Column("simple_model", sa.String(length=100), nullable=True),
        sa.Column("standard_model", sa.String(length=100), nullable=True),
        sa.Column("complex_model", sa.String(length=100), nullable=True),
        sa.Column("routing_rules", sa.JSON(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_routing_configs_user_id",
        "model_routing_configs",
        ["user_id"],
    )
