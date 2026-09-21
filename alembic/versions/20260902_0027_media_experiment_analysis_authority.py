"""Add versioned experiment-analysis authority and immutable input lineage.

Experiment results previously stored only an aggregate JSON payload.  This
revision makes the analysis method/version and design explicit, records
assignment/exposure evidence, and adds an append-only child ledger binding
each result to the exact ``MetricSnapshot`` hash used as an input.

The same revision also repairs the foreign-key/check-constraint parity of the
WS8 learning-review columns and installs database guards for the three
historical evidence ledgers that were append-only by application convention
but lacked a database-level guard.  Experiment *definitions* remain mutable
for their draft/running/completed status transitions.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0027"
down_revision = "20260902_0026"
branch_labels = None
depends_on = None


_RESULT_TABLE = "media_experiment_results"
_INPUT_TABLE = "media_experiment_result_metric_inputs"
_METRIC_TABLE = "media_metric_snapshots"
_REVENUE_TABLE = "media_revenue_events"
_LEARNING_TABLE = "media_learning_proposals"


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _dialect_name() -> str:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower()


def _is_sqlite() -> bool:
    return _dialect_name() == "sqlite"


def _add_result_columns() -> None:
    columns = (
        sa.Column(
            "analysis_method",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'normalized_metric_comparison'"),
        ),
        sa.Column(
            "analysis_version",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'1'"),
        ),
        sa.Column(
            "analysis_design",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'observational'"),
        ),
        sa.Column(
            "assignment_evidence",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "exposure_evidence",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    checks = (
        sa.CheckConstraint(
            "length(trim(analysis_method)) > 0",
            name="ck_media_experiment_results_analysis_method",
        ),
        sa.CheckConstraint(
            "length(trim(analysis_version)) > 0",
            name="ck_media_experiment_results_analysis_version",
        ),
        sa.CheckConstraint(
            "analysis_design IN ('controlled', 'observational')",
            name="ck_media_experiment_results_analysis_design",
        ),
    )
    if _is_sqlite():
        with op.batch_alter_table(_RESULT_TABLE, recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
            for check in checks:
                batch.create_check_constraint(check.name, check.sqltext)
    else:
        for column in columns:
            op.add_column(_RESULT_TABLE, column)
        for check in checks:
            op.create_check_constraint(check.name, _RESULT_TABLE, check.sqltext)


def _drop_result_columns() -> None:
    names = (
        "exposure_evidence",
        "assignment_evidence",
        "analysis_design",
        "analysis_version",
        "analysis_method",
    )
    checks = (
        "ck_media_experiment_results_analysis_design",
        "ck_media_experiment_results_analysis_version",
        "ck_media_experiment_results_analysis_method",
    )
    if _is_sqlite():
        with op.batch_alter_table(_RESULT_TABLE, recreate="always") as batch:
            for name in checks:
                batch.drop_constraint(name, type_="check")
            for name in names:
                batch.drop_column(name)
    else:
        for name in checks:
            op.drop_constraint(name, _RESULT_TABLE, type_="check")
        for name in names:
            op.drop_column(_RESULT_TABLE, name)


def _repair_learning_constraints() -> None:
    expected_fk = "fk_media_learning_proposals_expected_persona_revision_id"
    applied_fk = "fk_media_learning_proposals_applied_persona_revision_id"
    expected_check = "ck_media_learning_proposals_expected_revision_hash"
    applied_check = "ck_media_learning_proposals_applied_revision_hash"
    if _is_sqlite():
        with op.batch_alter_table(_LEARNING_TABLE, recreate="always") as batch:
            batch.create_foreign_key(
                expected_fk,
                "media_persona_revisions",
                ["expected_persona_revision_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_foreign_key(
                applied_fk,
                "media_persona_revisions",
                ["applied_persona_revision_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_check_constraint(
                expected_check,
                "expected_persona_revision_hash IS NULL OR length(expected_persona_revision_hash) = 64",
            )
            batch.create_check_constraint(
                applied_check,
                "applied_persona_revision_hash IS NULL OR length(applied_persona_revision_hash) = 64",
            )
    else:
        op.create_foreign_key(
            expected_fk,
            _LEARNING_TABLE,
            "media_persona_revisions",
            ["expected_persona_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            applied_fk,
            _LEARNING_TABLE,
            "media_persona_revisions",
            ["applied_persona_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_check_constraint(
            expected_check,
            _LEARNING_TABLE,
            "expected_persona_revision_hash IS NULL OR length(expected_persona_revision_hash) = 64",
        )
        op.create_check_constraint(
            applied_check,
            _LEARNING_TABLE,
            "applied_persona_revision_hash IS NULL OR length(applied_persona_revision_hash) = 64",
        )


def _drop_learning_constraints() -> None:
    expected_fk = "fk_media_learning_proposals_expected_persona_revision_id"
    applied_fk = "fk_media_learning_proposals_applied_persona_revision_id"
    expected_check = "ck_media_learning_proposals_expected_revision_hash"
    applied_check = "ck_media_learning_proposals_applied_revision_hash"
    if _is_sqlite():
        with op.batch_alter_table(_LEARNING_TABLE, recreate="always") as batch:
            for name in (applied_check, expected_check):
                batch.drop_constraint(name, type_="check")
            for name in (applied_fk, expected_fk):
                batch.drop_constraint(name, type_="foreignkey")
    else:
        for name in (applied_check, expected_check):
            op.drop_constraint(name, _LEARNING_TABLE, type_="check")
        for name in (applied_fk, expected_fk):
            op.drop_constraint(name, _LEARNING_TABLE, type_="foreignkey")


def _create_input_table() -> None:
    op.create_table(
        _INPUT_TABLE,
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("experiment_result_id", _uuid(), nullable=False),
        sa.Column("metric_snapshot_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("metric_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("group_name", sa.String(length=64), nullable=False),
        sa.Column("variant_ref", sa.String(length=164), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_result_id"],
            [_RESULT_TABLE + ".id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metric_snapshot_id"],
            [_METRIC_TABLE + ".id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "length(metric_snapshot_hash) = 64",
            name="ck_media_experiment_result_metric_inputs_snapshot_hash",
        ),
        sa.CheckConstraint(
            "length(trim(group_name)) > 0",
            name="ck_media_experiment_result_metric_inputs_group_name",
        ),
        sa.CheckConstraint(
            "variant_ref IS NULL OR length(trim(variant_ref)) > 0",
            name="ck_media_experiment_result_metric_inputs_variant_ref",
        ),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 1000000",
            name="ck_media_experiment_result_metric_inputs_ordinal",
        ),
        sa.UniqueConstraint(
            "experiment_result_id",
            "ordinal",
            name="uq_media_experiment_result_metric_inputs_ordinal",
        ),
        sa.UniqueConstraint(
            "experiment_result_id",
            "metric_snapshot_id",
            name="uq_media_experiment_result_metric_inputs_snapshot",
        ),
    )
    for name, columns in {
        "ix_media_experiment_result_inputs_result_id": ["experiment_result_id"],
        "ix_media_experiment_result_inputs_snapshot_id": ["metric_snapshot_id"],
        "ix_media_experiment_result_inputs_owner_id": ["owner_user_id"],
        "ix_media_experiment_result_inputs_project_id": ["project_id"],
        "ix_media_experiment_result_inputs_snapshot_hash": ["metric_snapshot_hash"],
        "ix_media_experiment_result_inputs_group_name": ["group_name"],
        "ix_media_experiment_result_inputs_variant_ref": ["variant_ref"],
        "ix_media_experiment_result_inputs_created_at": ["created_at"],
        "ix_media_experiment_result_inputs_owner_project": [
            "owner_user_id",
            "project_id",
        ],
    }.items():
        op.create_index(name, _INPUT_TABLE, columns)


def _drop_input_table() -> None:
    for name in (
        "ix_media_experiment_result_inputs_owner_project",
        "ix_media_experiment_result_inputs_created_at",
        "ix_media_experiment_result_inputs_variant_ref",
        "ix_media_experiment_result_inputs_group_name",
        "ix_media_experiment_result_inputs_snapshot_hash",
        "ix_media_experiment_result_inputs_project_id",
        "ix_media_experiment_result_inputs_owner_id",
        "ix_media_experiment_result_inputs_snapshot_id",
        "ix_media_experiment_result_inputs_result_id",
    ):
        op.drop_index(name, table_name=_INPUT_TABLE)
    op.drop_table(_INPUT_TABLE)


def _install_append_only_guard(table_name: str) -> None:
    """Install per-ledger UPDATE/DELETE guards and a PostgreSQL TRUNCATE guard."""

    function_name = f"{table_name}_immutable"
    update_trigger = f"{table_name}_no_update"
    delete_trigger = f"{table_name}_no_delete"
    truncate_trigger = f"{table_name}_no_truncate"
    if _dialect_name() == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {function_name}()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION '{table_name} rows are immutable';
                END;
                $$;
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {update_trigger}
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION {function_name}();
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {truncate_trigger}
                BEFORE TRUNCATE ON {table_name}
                FOR EACH STATEMENT EXECUTE FUNCTION {function_name}();
                """
            )
        )
    elif _dialect_name() == "sqlite":
        # SQLite has no TRUNCATE statement/trigger.  UPDATE and DELETE cover
        # every row-level mutation available on that backend.
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {update_trigger}
                BEFORE UPDATE ON {table_name}
                BEGIN
                  SELECT RAISE(ABORT, '{table_name} rows are immutable');
                END;
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {delete_trigger}
                BEFORE DELETE ON {table_name}
                BEGIN
                  SELECT RAISE(ABORT, '{table_name} rows are immutable');
                END;
                """
            )
        )


def _remove_append_only_guard(table_name: str) -> None:
    function_name = f"{table_name}_immutable"
    update_trigger = f"{table_name}_no_update"
    delete_trigger = f"{table_name}_no_delete"
    truncate_trigger = f"{table_name}_no_truncate"
    if _dialect_name() == "postgresql":
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {update_trigger} ON {table_name}"
            )
        )
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {truncate_trigger} ON {table_name}"
            )
        )
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function_name}()"))
    elif _dialect_name() == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {update_trigger}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {delete_trigger}"))


def upgrade() -> None:
    _add_result_columns()
    _repair_learning_constraints()
    _create_input_table()
    for table_name in (_METRIC_TABLE, _REVENUE_TABLE, _RESULT_TABLE, _INPUT_TABLE):
        _install_append_only_guard(table_name)


def downgrade() -> None:
    for table_name in (_INPUT_TABLE, _RESULT_TABLE, _REVENUE_TABLE, _METRIC_TABLE):
        _remove_append_only_guard(table_name)
    _drop_input_table()
    _drop_learning_constraints()
    _drop_result_columns()
