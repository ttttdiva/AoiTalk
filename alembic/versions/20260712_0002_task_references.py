"""タスクの汎用Referencesを追加"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260712_0002"
down_revision = "20260712_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference_type", sa.String(length=80), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False, server_default="related"),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("target_path", sa.Text(), nullable=True),
        sa.Column("target_url", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("dedupe_key", sa.String(length=1200), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint(
            "task_id", "reference_type", "relation_type", "dedupe_key",
            name="uq_task_references_target",
        ),
    )
    op.create_index("ix_task_references_task_id", "task_references", ["task_id"])
    op.create_index("ix_task_references_project_id", "task_references", ["project_id"])
    op.create_index("ix_task_references_created_by", "task_references", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_task_references_created_by", table_name="task_references")
    op.drop_index("ix_task_references_project_id", table_name="task_references")
    op.drop_index("ix_task_references_task_id", table_name="task_references")
    op.drop_table("task_references")
