"""Add durable Heartbeat scheduler run state.

Revision ID: 20260827_0002
Revises: 20260827_0001
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260827_0002"
down_revision = "20260827_0001"
branch_labels = None
depends_on = None


class _MigrationFailClosed(RuntimeError):
    """Abort when durable Heartbeat state cannot be created safely."""


def _require_online_migration() -> None:
    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        raise _MigrationFailClosed(
            "20260827_0002 cannot run in offline --sql mode; "
            "Heartbeat durable state creation requires live schema validation"
        )


def upgrade() -> None:
    _require_online_migration()

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "projects" not in table_names:
        raise _MigrationFailClosed(
            "required projects table is missing; "
            "heartbeat_run_states was not created"
        )

    if "heartbeat_run_states" in table_names:
        raise _MigrationFailClosed(
            "heartbeat_run_states unexpectedly already exists; "
            "refusing to overwrite or reinterpret existing scheduler state"
        )

    op.create_table(
        "heartbeat_run_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_name",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column(
            "scope_type",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "scope_id",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "last_started_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_completed_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_success_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "next_due_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "cursor_json",
            sa.JSON(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "heartbeat_name <> ''",
            name="ck_heartbeat_run_states_heartbeat_name",
        ),
        sa.CheckConstraint(
            "scope_type IN ('project', 'global')",
            name="ck_heartbeat_run_states_scope_type",
        ),
        sa.CheckConstraint(
            "("
            "scope_type = 'project' AND project_id IS NOT NULL AND scope_id <> ''"
            ") OR ("
            "scope_type = 'global' AND project_id IS NULL AND scope_id = 'global'"
            ")",
            name="ck_heartbeat_run_states_scope_identity",
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'running', 'failed')",
            name="ck_heartbeat_run_states_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "heartbeat_name",
            "scope_type",
            "scope_id",
            name="uq_heartbeat_run_states_heartbeat_scope",
        ),
    )

    op.create_index(
        "ix_heartbeat_run_states_project_id",
        "heartbeat_run_states",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_heartbeat_run_states_next_due_at",
        "heartbeat_run_states",
        ["next_due_at"],
        unique=False,
    )
    op.create_index(
        "ix_heartbeat_run_states_status_next_due_at",
        "heartbeat_run_states",
        ["status", "next_due_at"],
        unique=False,
    )

    refreshed = sa.inspect(bind)
    if "heartbeat_run_states" not in set(refreshed.get_table_names()):
        raise _MigrationFailClosed(
            "heartbeat_run_states creation could not be verified"
        )

    columns = {
        column["name"]
        for column in refreshed.get_columns("heartbeat_run_states")
    }
    required_columns = {
        "id",
        "heartbeat_name",
        "scope_type",
        "scope_id",
        "project_id",
        "last_started_at",
        "last_completed_at",
        "last_success_at",
        "next_due_at",
        "status",
        "error_message",
        "cursor_json",
        "created_at",
        "updated_at",
    }
    missing_columns = sorted(required_columns - columns)
    if missing_columns:
        raise _MigrationFailClosed(
            "heartbeat_run_states creation verification failed; "
            "missing columns: "
            + ", ".join(missing_columns)
        )

    indexes = {
        index["name"]
        for index in refreshed.get_indexes("heartbeat_run_states")
    }
    required_indexes = {
        "ix_heartbeat_run_states_project_id",
        "ix_heartbeat_run_states_next_due_at",
        "ix_heartbeat_run_states_status_next_due_at",
    }
    missing_indexes = sorted(required_indexes - indexes)
    if missing_indexes:
        raise _MigrationFailClosed(
            "heartbeat_run_states creation verification failed; "
            "missing indexes: "
            + ", ".join(missing_indexes)
        )

    unique_constraints = {
        constraint["name"]
        for constraint in refreshed.get_unique_constraints(
            "heartbeat_run_states"
        )
    }
    if (
        "uq_heartbeat_run_states_heartbeat_scope"
        not in unique_constraints
    ):
        raise _MigrationFailClosed(
            "heartbeat_run_states creation verification failed; "
            "heartbeat scope unique constraint is missing"
        )


def downgrade() -> None:
    raise _MigrationFailClosed(
        "20260827_0002 is intentionally irreversible. "
        "HeartbeatRunState is durable scheduler ownership/cursor state; "
        "dropping it can cause duplicate or replayed background execution. "
        "Restore a pre-migration database backup instead."
    )
