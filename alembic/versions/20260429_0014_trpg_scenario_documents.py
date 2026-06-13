"""Add TRPG scenario document table.

Revision ID: 20260429_0014
Revises: 20260429_0013
Create Date: 2026-04-29 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260429_0014"
down_revision = "20260429_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trpg_scenario_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ruleset", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("source_label", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "structure",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_trpg_scenario_documents_scenario_id",
        "trpg_scenario_documents",
        ["scenario_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trpg_scenario_documents_scenario_id",
        table_name="trpg_scenario_documents",
    )
    op.drop_table("trpg_scenario_documents")
