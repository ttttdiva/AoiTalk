"""Drop inbox_items table and tasks.rollup_bucket column.

Inbox機能はユーザーごとのInboxスペースに統合。
rollup_bucketはスペース分離で不要になったため削除。

Revision ID: 20260416_0003
Revises: 20260416_0002
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260416_0003"
down_revision = "20260416_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # inbox_items テーブルを削除
    op.drop_index("ix_inbox_items_updated_at", table_name="inbox_items")
    op.drop_index("ix_inbox_items_bucket", table_name="inbox_items")
    op.drop_index("ix_inbox_items_owner_id", table_name="inbox_items")
    op.drop_table("inbox_items")

    # rollup_bucket カラムを削除
    op.drop_index("ix_tasks_rollup_bucket", table_name="tasks")
    op.drop_column("tasks", "rollup_bucket")


def downgrade() -> None:
    # rollup_bucket カラムを復元
    op.add_column(
        "tasks",
        sa.Column(
            "rollup_bucket",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index("ix_tasks_rollup_bucket", "tasks", ["rollup_bucket"])

    # inbox_items テーブルを復元
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
