"""Add release-qualified App action activation and run fences."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261018_0001"
down_revision = "20261017_0001"
branch_labels = None
depends_on = None


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        primary_key=name == "id",
    )


def upgrade() -> None:
    op.create_table(
        "app_runtime_activations",
        sa.Column(
            "app_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "release_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "activated_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "activated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name="fk_app_runtime_activations_app",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_runtime_activations_release_app",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by"],
            ["users.id"],
            name="fk_app_runtime_activations_activated_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64",
            name="ck_app_runtime_activations_manifest_hash",
        ),
    )
    op.create_index(
        "ix_app_runtime_activations_release_id",
        "app_runtime_activations",
        ["release_id"],
    )
    op.create_index(
        "ix_app_runtime_activations_activated_by",
        "app_runtime_activations",
        ["activated_by"],
    )
    op.create_index(
        "ix_app_runtime_activations_activated_at",
        "app_runtime_activations",
        ["activated_at"],
    )

    op.create_table(
        "app_action_runs",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_key", sa.String(length=128), nullable=False),
        sa.Column("action_ref", sa.String(length=220), nullable=False),
        sa.Column("action_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("target_key", sa.String(length=80), nullable=False),
        sa.Column("execution_target_id", sa.String(length=128), nullable=False),
        sa.Column("execution_target_kind", sa.String(length=32), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("machine_id", sa.String(length=128), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("saved_action_id", sa.String(length=128), nullable=True),
        sa.Column("target_sha256", sa.String(length=64), nullable=True),
        sa.Column("target_contract_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "probe_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "input_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "started_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column(
            "dispatch_state",
            sa.String(length=16),
            nullable=False,
            server_default="not_sent",
        ),
        sa.Column(
            "readiness_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "process_status",
            sa.String(length=16),
            nullable=False,
            server_default="not_started",
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "result_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("log_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connection_epoch", sa.String(length=128), nullable=True),
        sa.Column("remote_journal_epoch", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("dispatch_started_at", sa.DateTime(), nullable=True),
        sa.Column("dispatch_sent_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name="fk_app_action_runs_app",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_app_action_runs_project",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_action_runs_release_app",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"],
            ["users.id"],
            name="fk_app_action_runs_started_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name="fk_app_action_runs_agent_run",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "app_id",
            "started_by",
            "idempotency_key",
            name="uq_app_action_runs_app_starter_idempotency",
        ),
        sa.CheckConstraint(
            "action_key <> '' AND length(action_key) <= 128",
            name="ck_app_action_runs_action_key",
        ),
        sa.CheckConstraint(
            "length(action_ref) BETWEEN 1 AND 220",
            name="ck_app_action_runs_action_ref",
        ),
        sa.CheckConstraint(
            "length(action_spec_hash) = 64",
            name="ck_app_action_runs_action_spec_hash",
        ),
        sa.CheckConstraint(
            "execution_target_kind IN ('server','pc_bridge')",
            name="ck_app_action_runs_execution_target_kind",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('finite','service','desktop')",
            name="ck_app_action_runs_execution_mode",
        ),
        sa.CheckConstraint(
            "length(machine_id) BETWEEN 1 AND 128",
            name="ck_app_action_runs_machine_id",
        ),
        sa.CheckConstraint(
            "length(device_id) BETWEEN 1 AND 128",
            name="ck_app_action_runs_device_id",
        ),
        sa.CheckConstraint(
            "target_sha256 IS NULL OR length(target_sha256) = 64",
            name="ck_app_action_runs_target_sha256",
        ),
        sa.CheckConstraint(
            "length(target_contract_hash) = 64",
            name="ck_app_action_runs_target_contract_hash",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64",
            name="ck_app_action_runs_input_hash",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_app_action_runs_idempotency_key",
        ),
        sa.CheckConstraint(
            "status IN ('queued','dispatching','accepted','starting','running','stopping','succeeded','failed','cancelled','blocked','unknown')",
            name="ck_app_action_runs_status",
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('not_sent','sent','accepted','unknown')",
            name="ck_app_action_runs_dispatch_state",
        ),
        sa.CheckConstraint(
            "readiness_status IN ('not_required','pending','ready','failed','unknown')",
            name="ck_app_action_runs_readiness_status",
        ),
        sa.CheckConstraint(
            "process_status IN ('not_started','running','exited','unknown')",
            name="ck_app_action_runs_process_status",
        ),
        sa.CheckConstraint(
            "log_cursor >= 0",
            name="ck_app_action_runs_log_cursor",
        ),
    )
    op.create_index("ix_app_action_runs_app_id", "app_action_runs", ["app_id"])
    op.create_index("ix_app_action_runs_project_id", "app_action_runs", ["project_id"])
    op.create_index("ix_app_action_runs_release_id", "app_action_runs", ["release_id"])
    op.create_index("ix_app_action_runs_started_by", "app_action_runs", ["started_by"])
    op.create_index("ix_app_action_runs_agent_run_id", "app_action_runs", ["agent_run_id"])
    op.create_index("ix_app_action_runs_status", "app_action_runs", ["status"])
    op.create_index("ix_app_action_runs_dispatch_state", "app_action_runs", ["dispatch_state"])
    op.create_index("ix_app_action_runs_readiness_status", "app_action_runs", ["readiness_status"])
    op.create_index("ix_app_action_runs_process_status", "app_action_runs", ["process_status"])
    op.create_index("ix_app_action_runs_created_at", "app_action_runs", ["created_at"])
    op.create_index(
        "ix_app_action_runs_app_status_created",
        "app_action_runs",
        ["app_id", "status", "created_at"],
    )
    op.create_index(
        "ix_app_action_runs_device_status_created",
        "app_action_runs",
        ["device_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_action_runs_device_status_created", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_app_status_created", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_created_at", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_process_status", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_readiness_status", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_dispatch_state", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_status", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_agent_run_id", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_started_by", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_release_id", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_project_id", table_name="app_action_runs")
    op.drop_index("ix_app_action_runs_app_id", table_name="app_action_runs")
    op.drop_table("app_action_runs")
    op.drop_index("ix_app_runtime_activations_activated_at", table_name="app_runtime_activations")
    op.drop_index("ix_app_runtime_activations_activated_by", table_name="app_runtime_activations")
    op.drop_index("ix_app_runtime_activations_release_id", table_name="app_runtime_activations")
    op.drop_table("app_runtime_activations")
