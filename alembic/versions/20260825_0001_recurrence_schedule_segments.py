"""Add canonical recurrence schedule segments and occurrence identity."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260825_0001"
down_revision = "20260824_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing materialized rows intentionally remain NULL.  Readers use the
    # legacy source_kind fallback until a row is rewritten or regenerated.
    op.add_column(
        "task_occurrences",
        sa.Column("original_start_at", sa.DateTime(), nullable=True),
    )
    # Segment offsets can legitimately make adjacent canonical occurrences
    # share an actual timestamp.  Replace the legacy actual-start uniqueness
    # with canonical/source identity so both rows remain materializable.
    op.drop_constraint(
        "unique_task_occurrence_start",
        "task_occurrences",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_task_occurrence_canonical_source",
        "task_occurrences",
        ["task_id", "original_start_at", "source_kind"],
    )

    op.create_table(
        "task_recurrence_schedule_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("start_offset_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_offset_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_recurrence_schedule_segments_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "effective_from",
            name="uq_task_recurrence_schedule_segments_task_boundary",
        ),
    )
    op.create_index(
        "ix_task_recurrence_schedule_segments_task_effective",
        "task_recurrence_schedule_segments",
        ["task_id", "effective_from"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_recurrence_schedule_segments_task_effective",
        table_name="task_recurrence_schedule_segments",
    )
    op.drop_table("task_recurrence_schedule_segments")
    op.drop_constraint(
        "uq_task_occurrence_canonical_source",
        "task_occurrences",
        type_="unique",
    )
    op.create_unique_constraint(
        "unique_task_occurrence_start",
        "task_occurrences",
        ["task_id", "start_at"],
    )
    op.drop_column("task_occurrences", "original_start_at")
