"""Add bounded durable Heartbeat execution history.

HeartbeatRunState remains the scheduler state machine.  This migration adds a
separate operational log containing only safe counters, status, continuation
boolean, and projected questions/summary; no cursor, evidence, prompt, tool
payload, or raw exception text is copied from a run.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_hb01"
down_revision = "20260901_0008"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _indexes(bind: sa.Connection, table_name: str) -> set[str]:
    if not _table_exists(bind, table_name):
        return set()
    return {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "heartbeat_run_history"):
        op.create_table(
            "heartbeat_run_history",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                nullable=False,
            ),
            sa.Column("heartbeat_name", sa.String(length=120), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("scope_type", sa.String(length=16), nullable=False),
            sa.Column("scope_id", sa.String(length=120), nullable=False),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'running'"),
            ),
            sa.Column("success", sa.Boolean(), nullable=True),
            sa.Column(
                "memory_upsert_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "forgotten_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "question_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "continuation_pending",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("questions_json", sa.JSON(), nullable=True),
            sa.Column("result_summary", sa.String(length=512), nullable=True),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.Column(
                "forced",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "generic_action_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "generic_action_failure_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "heartbeat_name <> ''",
                name="ck_heartbeat_run_history_heartbeat_name",
            ),
            sa.CheckConstraint(
                "mode IN ('agent_check', 'project_steward')",
                name="ck_heartbeat_run_history_mode",
            ),
            sa.CheckConstraint(
                "scope_type IN ('project', 'global')",
                name="ck_heartbeat_run_history_scope_type",
            ),
            sa.CheckConstraint(
                "(scope_type = 'project' AND project_id IS NOT NULL AND scope_id <> '') "
                "OR (scope_type = 'global' AND project_id IS NULL AND scope_id = 'global')",
                name="ck_heartbeat_run_history_scope_identity",
            ),
            sa.CheckConstraint(
                "status IN ('running', 'succeeded', 'failed', 'stale')",
                name="ck_heartbeat_run_history_status",
            ),
            sa.CheckConstraint(
                "memory_upsert_count >= 0 AND memory_upsert_count <= 100000",
                name="ck_heartbeat_run_history_memory_count",
            ),
            sa.CheckConstraint(
                "forgotten_count >= 0 AND forgotten_count <= 100000",
                name="ck_heartbeat_run_history_forgotten_count",
            ),
            sa.CheckConstraint(
                "question_count >= 0 AND question_count <= 100000",
                name="ck_heartbeat_run_history_question_count",
            ),
            sa.CheckConstraint(
                "generic_action_count >= 0 AND generic_action_count <= 100000",
                name="ck_heartbeat_run_history_action_count",
            ),
            sa.CheckConstraint(
                "generic_action_failure_count >= 0 AND generic_action_failure_count <= generic_action_count",
                name="ck_heartbeat_run_history_action_failure_count",
            ),
        )

    # Named indexes are installed separately so an interrupted/partially
    # provisioned migration can be retried without duplicate-index errors.
    for name, columns in (
        (
            "ix_heartbeat_run_history_heartbeat_started",
            ["heartbeat_name", "started_at", "id"],
        ),
        (
            "ix_heartbeat_run_history_scope_started",
            ["scope_type", "scope_id", "started_at", "id"],
        ),
        (
            "ix_heartbeat_run_history_project_started",
            ["project_id", "started_at", "id"],
        ),
        ("ix_heartbeat_run_history_status", ["status"]),
    ):
        if name not in _indexes(bind, "heartbeat_run_history"):
            op.create_index(name, "heartbeat_run_history", columns)


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "heartbeat_run_history"):
        return
    for name in (
        "ix_heartbeat_run_history_status",
        "ix_heartbeat_run_history_project_started",
        "ix_heartbeat_run_history_scope_started",
        "ix_heartbeat_run_history_heartbeat_started",
    ):
        if name in _indexes(bind, "heartbeat_run_history"):
            op.drop_index(name, table_name="heartbeat_run_history")
    op.drop_table("heartbeat_run_history")
