"""Add project task schedule phases and placements.

Revision ID: 20260810_0001
Revises: 20260809_0023

The schedule canvas stores phase dates and task placement coordinates separately
from ``tasks.start_at``/``tasks.end_at``.  A task has at most one placement;
deleting a phase intentionally keeps the placement row and clears its phase so
the task remains visible in the unplaced shelf.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260810_0001"
down_revision = "20260809_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_schedule_phases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("start_on", sa.Date(), nullable=False),
        sa.Column("end_on", sa.Date(), nullable=False),
        sa.Column(
            "sort_order",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
            "end_on >= start_on",
            name="ck_project_schedule_phases_date_range",
        ),
    )
    op.create_index(
        "ix_project_schedule_phases_project_id",
        "project_schedule_phases",
        ["project_id"],
    )
    op.create_index(
        "ix_project_schedule_phases_project_sort",
        "project_schedule_phases",
        ["project_id", "sort_order", "start_on"],
    )

    op.create_table(
        "task_schedule_placements",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "phase_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_schedule_phases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "x_ratio",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "y",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
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
            "x_ratio = x_ratio AND x_ratio >= 0 AND x_ratio <= 1",
            name="ck_task_schedule_placements_x_ratio",
        ),
        sa.CheckConstraint(
            "y = y AND y >= -100000 AND y <= 100000",
            name="ck_task_schedule_placements_y",
        ),
    )
    op.create_index(
        "ix_task_schedule_placements_phase_id",
        "task_schedule_placements",
        ["phase_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_schedule_placements_phase_id",
        table_name="task_schedule_placements",
    )
    op.drop_table("task_schedule_placements")
    op.drop_index(
        "ix_project_schedule_phases_project_sort",
        table_name="project_schedule_phases",
    )
    op.drop_index(
        "ix_project_schedule_phases_project_id",
        table_name="project_schedule_phases",
    )
    op.drop_table("project_schedule_phases")
