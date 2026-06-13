"""Add per-user inbox items table.

Revision ID: 20260416_0001
Revises: 20260415_0001
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260416_0001"
down_revision = "20260415_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
          "owner_id",
          UUID(as_uuid=True),
          sa.ForeignKey("users.id", ondelete="CASCADE"),
          nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("bucket", sa.String(20), nullable=False, server_default="inbox"),
        sa.Column(
            "promoted_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
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
    op.create_index("ix_inbox_items_owner_id", "inbox_items", ["owner_id"])
    op.create_index("ix_inbox_items_bucket", "inbox_items", ["bucket"])
    op.create_index("ix_inbox_items_updated_at", "inbox_items", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_inbox_items_updated_at", table_name="inbox_items")
    op.drop_index("ix_inbox_items_bucket", table_name="inbox_items")
    op.drop_index("ix_inbox_items_owner_id", table_name="inbox_items")
    op.drop_table("inbox_items")
