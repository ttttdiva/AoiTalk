"""Add space_id column to tags (keep project_id for back-compat).

Revision ID: 20260420_0003
Revises: 20260416_0003
Create Date: 2026-04-20 11:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260420_0003"
down_revision = "20260416_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tags",
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE tags
        SET space_id = projects.space_id
        FROM projects
        WHERE tags.project_id = projects.id
          AND projects.space_id IS NOT NULL
        """
    )
    op.create_index("ix_tags_space_id", "tags", ["space_id"])


def downgrade() -> None:
    op.drop_index("ix_tags_space_id", table_name="tags")
    op.drop_column("tags", "space_id")
