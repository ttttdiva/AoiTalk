"""タスク同士の対称な関連付けを追加"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260731_0001"
down_revision = "20260729_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_a_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_b_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "relation_type",
            sa.String(length=32),
            nullable=False,
            server_default="related",
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["task_a_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_b_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "task_a_id < task_b_id",
            name="ck_task_relations_canonical_order",
        ),
        sa.UniqueConstraint(
            "task_a_id",
            "task_b_id",
            "relation_type",
            name="uq_task_relations_pair",
        ),
    )
    op.create_index(
        "ix_task_relations_task_a_id", "task_relations", ["task_a_id"]
    )
    op.create_index(
        "ix_task_relations_task_b_id", "task_relations", ["task_b_id"]
    )
    op.create_index(
        "ix_task_relations_created_by", "task_relations", ["created_by"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_relations_created_by", table_name="task_relations")
    op.drop_index("ix_task_relations_task_b_id", table_name="task_relations")
    op.drop_index("ix_task_relations_task_a_id", table_name="task_relations")
    op.drop_table("task_relations")
