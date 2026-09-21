"""Add explicit MediaOps provider-execution authority to the action kernel.

The existing ``ExternalAction``/``ExternalActionAttempt`` rows remain the
single durable operation ledger.  This revision adds only bounded, opaque
execution metadata: a provider action is pinned to one immutable capability
snapshot, credential state hash and adapter version.  No credential body,
provider response or filesystem path is stored here.

All existing engagement/manual rows are backfilled with ``manual`` defaults.
Provider rows are accepted by the database only when the complete evidence
tuple is present; the service performs the stronger cross-row and registry
checks immediately before a provider call.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0024"
down_revision = "20260902_0023"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


_ACTION_COLUMNS = (
    sa.Column(
        "execution_mode",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'manual'"),
    ),
    sa.Column("capability_snapshot_id", _uuid(), nullable=True),
    sa.Column("capability_snapshot_hash", sa.String(length=64), nullable=True),
    sa.Column("adapter_key", sa.String(length=128), nullable=True),
    sa.Column("adapter_version", sa.String(length=32), nullable=True),
    sa.Column("credential_state_hash", sa.String(length=64), nullable=True),
    sa.Column("execution_key", sa.String(length=255), nullable=True),
)

_ATTEMPT_COLUMNS = (
    sa.Column(
        "execution_mode",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'manual'"),
    ),
    sa.Column("provider_key", sa.String(length=16), nullable=True),
    sa.Column("provider_adapter_key", sa.String(length=128), nullable=True),
    sa.Column("provider_adapter_version", sa.String(length=32), nullable=True),
    sa.Column("capability_snapshot_id", _uuid(), nullable=True),
    sa.Column("capability_snapshot_hash", sa.String(length=64), nullable=True),
    sa.Column("credential_state_hash", sa.String(length=64), nullable=True),
    sa.Column("execution_key", sa.String(length=255), nullable=True),
)


def _sqlite_alter_action() -> None:
    with op.batch_alter_table("external_actions", recreate="always") as batch:
        for column in _ACTION_COLUMNS:
            batch.add_column(column.copy())
        batch.create_check_constraint(
            "ck_external_actions_execution_mode",
            "execution_mode IN ('manual', 'provider')",
        )
        batch.create_check_constraint(
            "ck_external_actions_provider_evidence",
            "execution_mode <> 'provider' OR "
            "(capability_snapshot_id IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64 "
            "AND length(trim(adapter_key)) > 0 "
            "AND length(trim(adapter_version)) > 0 "
            "AND length(credential_state_hash) = 64 "
            "AND length(trim(execution_key)) > 0)",
        )
        batch.create_check_constraint(
            "ck_external_actions_capability_snapshot_hash",
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
        )
        batch.create_check_constraint(
            "ck_external_actions_credential_state_hash",
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
        )


def _sqlite_alter_attempt() -> None:
    with op.batch_alter_table("external_action_attempts", recreate="always") as batch:
        # The original model constraint is named and can be dropped while the
        # table is rebuilt.  SQLite does not support ALTER CONSTRAINT in place.
        batch.drop_constraint("ck_external_action_attempts_executor_type", type_="check")
        batch.create_check_constraint(
            "ck_external_action_attempts_executor_type",
            "executor_type IN ('manual', 'provider')",
        )
        for column in _ATTEMPT_COLUMNS:
            batch.add_column(column.copy())
        batch.create_check_constraint(
            "ck_external_action_attempts_execution_mode",
            "execution_mode IN ('manual', 'provider')",
        )
        batch.create_check_constraint(
            "ck_external_action_attempts_provider_executor",
            "execution_mode <> 'provider' OR executor_type = 'provider'",
        )
        batch.create_check_constraint(
            "ck_external_action_attempts_provider_evidence",
            "execution_mode <> 'provider' OR "
            "(length(trim(provider_key)) > 0 "
            "AND length(trim(provider_adapter_key)) > 0 "
            "AND length(trim(provider_adapter_version)) > 0 "
            "AND capability_snapshot_id IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64 "
            "AND length(credential_state_hash) = 64 "
            "AND length(trim(execution_key)) > 0)",
        )
        batch.create_check_constraint(
            "ck_external_action_attempts_capability_snapshot_hash",
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
        )
        batch.create_check_constraint(
            "ck_external_action_attempts_credential_state_hash",
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
        )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "sqlite":
        _sqlite_alter_action()
        _sqlite_alter_attempt()
    else:
        for column in _ACTION_COLUMNS:
            op.add_column("external_actions", column.copy())
        op.create_check_constraint(
            "ck_external_actions_execution_mode",
            "external_actions",
            "execution_mode IN ('manual', 'provider')",
        )
        op.create_check_constraint(
            "ck_external_actions_provider_evidence",
            "external_actions",
            "execution_mode <> 'provider' OR "
            "(capability_snapshot_id IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64 "
            "AND length(trim(adapter_key)) > 0 "
            "AND length(trim(adapter_version)) > 0 "
            "AND length(credential_state_hash) = 64 "
            "AND length(trim(execution_key)) > 0)",
        )
        op.create_check_constraint(
            "ck_external_actions_capability_snapshot_hash",
            "external_actions",
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
        )
        op.create_check_constraint(
            "ck_external_actions_credential_state_hash",
            "external_actions",
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
        )

        for column in _ATTEMPT_COLUMNS:
            op.add_column("external_action_attempts", column.copy())
        op.drop_constraint(
            "ck_external_action_attempts_executor_type",
            "external_action_attempts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_executor_type",
            "external_action_attempts",
            "executor_type IN ('manual', 'provider')",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_execution_mode",
            "external_action_attempts",
            "execution_mode IN ('manual', 'provider')",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_provider_executor",
            "external_action_attempts",
            "execution_mode <> 'provider' OR executor_type = 'provider'",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_provider_evidence",
            "external_action_attempts",
            "execution_mode <> 'provider' OR "
            "(length(trim(provider_key)) > 0 "
            "AND length(trim(provider_adapter_key)) > 0 "
            "AND length(trim(provider_adapter_version)) > 0 "
            "AND capability_snapshot_id IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64 "
            "AND length(credential_state_hash) = 64 "
            "AND length(trim(execution_key)) > 0)",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_capability_snapshot_hash",
            "external_action_attempts",
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
        )
        op.create_check_constraint(
            "ck_external_action_attempts_credential_state_hash",
            "external_action_attempts",
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
        )

    op.create_index(
        "ix_external_actions_capability_snapshot_id",
        "external_actions",
        ["capability_snapshot_id"],
    )
    op.create_index(
        "ix_external_action_attempts_capability_snapshot_id",
        "external_action_attempts",
        ["capability_snapshot_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    for name, table in (
        ("ix_external_action_attempts_capability_snapshot_id", "external_action_attempts"),
        ("ix_external_actions_capability_snapshot_id", "external_actions"),
    ):
        op.drop_index(name, table_name=table)

    if dialect == "sqlite":
        with op.batch_alter_table("external_action_attempts", recreate="always") as batch:
            for name in (
                "ck_external_action_attempts_credential_state_hash",
                "ck_external_action_attempts_capability_snapshot_hash",
                "ck_external_action_attempts_provider_evidence",
                "ck_external_action_attempts_provider_executor",
                "ck_external_action_attempts_execution_mode",
                "ck_external_action_attempts_executor_type",
            ):
                try:
                    batch.drop_constraint(name, type_="check")
                except Exception:
                    pass
            batch.create_check_constraint(
                "ck_external_action_attempts_executor_type",
                "executor_type = 'manual'",
            )
            for column in _ATTEMPT_COLUMNS:
                batch.drop_column(column.name)
        with op.batch_alter_table("external_actions", recreate="always") as batch:
            for name in (
                "ck_external_actions_credential_state_hash",
                "ck_external_actions_capability_snapshot_hash",
                "ck_external_actions_provider_evidence",
                "ck_external_actions_execution_mode",
            ):
                try:
                    batch.drop_constraint(name, type_="check")
                except Exception:
                    pass
            for column in _ACTION_COLUMNS:
                batch.drop_column(column.name)
    else:
        for name in (
            "ck_external_action_attempts_credential_state_hash",
            "ck_external_action_attempts_capability_snapshot_hash",
            "ck_external_action_attempts_provider_evidence",
            "ck_external_action_attempts_provider_executor",
            "ck_external_action_attempts_execution_mode",
            "ck_external_action_attempts_executor_type",
        ):
            op.drop_constraint(name, "external_action_attempts", type_="check")
        op.create_check_constraint(
            "ck_external_action_attempts_executor_type",
            "external_action_attempts",
            "executor_type = 'manual'",
        )
        for column in reversed(_ATTEMPT_COLUMNS):
            op.drop_column("external_action_attempts", column.name)
        for name in (
            "ck_external_actions_credential_state_hash",
            "ck_external_actions_capability_snapshot_hash",
            "ck_external_actions_provider_evidence",
            "ck_external_actions_execution_mode",
        ):
            op.drop_constraint(name, "external_actions", type_="check")
        for column in reversed(_ACTION_COLUMNS):
            op.drop_column("external_actions", column.name)
