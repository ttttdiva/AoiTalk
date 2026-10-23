"""Persist idempotency results for Task API operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20261023_0001"
down_revision = "20261022_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_api_operation_receipts",
        sa.Column(
            "actor_id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("operation_id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("op", sa.String(length=16), nullable=False),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_task_api_operation_receipts_task_id",
        "task_api_operation_receipts",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_api_operation_receipts_task_id",
        table_name="task_api_operation_receipts",
    )
    op.drop_table("task_api_operation_receipts")
