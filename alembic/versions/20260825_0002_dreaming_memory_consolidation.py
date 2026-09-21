"""Add durable Dreaming Memory consolidation state and run ledgers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260825_0002"
down_revision = "20260825_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dreaming_memory_states",
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("backfill_before_at", sa.DateTime(), nullable=True),
        sa.Column(
            "backfill_before_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "backfill_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_incremental_at", sa.DateTime(), nullable=True),
        sa.Column("last_dreamed_at", sa.DateTime(), nullable=True),
        sa.Column("last_history_digest", sa.String(length=64), nullable=True),
        sa.Column("last_full_reconcile_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "dreaming_memory_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "source_message_ids",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "source_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("source_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "backfill",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "candidate_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "mutation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "trigger IN ('idle', 'startup', 'reconcile')",
            name="ck_dreaming_memory_runs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'skipped', 'failed')",
            name="ck_dreaming_memory_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dreaming_memory_runs_user_id",
        "dreaming_memory_runs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_dreaming_memory_runs_user_status_created",
        "dreaming_memory_runs",
        ["user_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dreaming_memory_runs_user_status_created",
        table_name="dreaming_memory_runs",
    )
    op.drop_index(
        "ix_dreaming_memory_runs_user_id",
        table_name="dreaming_memory_runs",
    )
    op.drop_table("dreaming_memory_runs")
    op.drop_table("dreaming_memory_states")
