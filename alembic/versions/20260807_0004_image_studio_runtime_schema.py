"""Add the durable Image Studio v2 runtime schema.

Revision ID: 20260807_0004_runtime
Revises: 20260807_0003
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260807_0004_runtime"
down_revision = "20260807_0003"
branch_labels = None
depends_on = None


# ``20260807_0004`` was used by two different revisions in the original
# checkout.  A database that already ran the Image Studio runtime migration
# therefore has an ambiguous ``alembic_version`` value.  Keep the runtime
# migration content intact, but make the upgrade idempotent for the one safe
# case: all runtime tables and the columns added to the legacy tables are
# present.  Any partial footprint is rejected instead of guessing which DDL
# statements were already committed.
_RUNTIME_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "image_studio_batches": frozenset(
        {
            "id",
            "project_id",
            "source_shot_id",
            "source_revision_id",
            "created_by",
            "title",
            "status",
            "render_plan_snapshot",
            "plan_hash",
            "request_key_hash",
            "request_fingerprint",
            "expected_run_count",
            "frozen_at",
            "cancel_requested_at",
            "created_at",
            "updated_at",
        }
    ),
    "image_studio_execution_manifests": frozenset(
        {
            "id",
            "project_id",
            "batch_id",
            "shot_id",
            "branch_id",
            "revision_id",
            "workflow_id",
            "created_by",
            "schema_version",
            "engine",
            "render_plan_snapshot",
            "manifest_payload",
            "manifest_hash",
            "workflow_key",
            "workflow_version",
            "workflow_hash",
            "api_graph_snapshot",
            "compiler_version",
            "compiler_hash",
            "adapter_version",
            "created_at",
        }
    ),
    "image_studio_runs": frozenset(
        {
            "id",
            "project_id",
            "batch_id",
            "manifest_id",
            "generation_id",
            "parent_run_id",
            "created_by",
            "logical_job_key",
            "attempt_no",
            "execution_key",
            "request_key_hash",
            "request_fingerprint",
            "status",
            "execution_stage",
            "recoverable",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "fence_version",
            "claim_attempts",
            "recovery_attempts",
            "next_reconcile_at",
            "engine_instance_key",
            "submission_nonce",
            "prompt_id",
            "expected_output_count",
            "ready_output_count",
            "cancel_requested_at",
            "cancel_outcome",
            "cancel_error",
            "result_summary",
            "error_code",
            "error_detail",
            "queued_at",
            "submitted_at",
            "started_at",
            "ended_at",
            "created_at",
            "updated_at",
        }
    ),
    "image_studio_run_events": frozenset(
        {
            "id",
            "run_id",
            "sequence",
            "event_type",
            "from_status",
            "to_status",
            "stage",
            "fence_version",
            "source",
            "evidence",
            "created_at",
        }
    ),
}

_RUNTIME_LEGACY_COLUMNS: dict[str, frozenset[str]] = {
    "image_studio_outputs": frozenset(
        {
            "run_id",
            "engine_output_key",
            "engine_locator",
            "ingest_state",
            "ingest_attempts",
            "ingest_error",
            "last_ingest_at",
        }
    ),
    "image_studio_assets": frozenset(
        {
            "storage_state",
            "width",
            "height",
            "thumbnail_asset_id",
            "delivery_key",
        }
    ),
    "image_studio_workflows": frozenset({"frozen_at", "supersedes_workflow_id"}),
}

# ``20260807_0004`` was historically shared by the canonical character
# dedupe and this runtime migration.  Keep a connection-local marker when we
# observe the already-complete runtime footprint so the terminal merge can
# distinguish that old ambiguous path from normal/dual-head upgrades.  The
# marker is deliberately not persisted in application tables.
_LEGACY_RUNTIME_AMBIGUITY_FLAG = "_aoi_image_studio_legacy_runtime_ambiguous"


def _runtime_already_applied() -> bool:
    """Return whether an old duplicate-0004 runtime footprint is complete.

    The normal pre-runtime schema has the three legacy tables in
    ``_RUNTIME_LEGACY_COLUMNS`` but none of the columns listed there.  Seeing
    only part of the new footprint means an interrupted/manual migration; it
    must fail closed rather than issuing duplicate DDL or silently accepting a
    schema that the following migrations cannot use.
    """

    try:
        bind = op.get_bind()
    except Exception:
        # Keep offline SQL generation and lightweight operation recorders
        # compatible.  A real Alembic online bind always exposes ``dialect``.
        return False
    if bind is None or not hasattr(bind, "dialect"):
        return False

    try:
        context = op.get_context()
    except Exception:
        context = None
    if getattr(context, "as_sql", False):
        # Offline rendering has no database to inspect; emit the original DDL
        # and let the online upgrade perform the ambiguity check.
        return False

    try:
        inspector = sa.inspect(bind)
        schema = None
        if hasattr(bind, "exec_driver_sql"):
            try:
                schema = bind.exec_driver_sql("SELECT current_schema()").scalar()
            except Exception:
                schema = None
        if schema is not None and hasattr(bind, "execute"):
            # SQLAlchemy's PostgreSQL inspector intentionally hides temporary
            # schemas.  Query information_schema directly so disposable
            # pg_temp validation and real online connections use the same
            # footprint check.
            table_rows = bind.execute(
                sa.text(
                    "SELECT table_name "
                    "FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            ).all()
            column_rows = bind.execute(
                sa.text(
                    "SELECT table_name, column_name "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            ).all()
            table_names = {row[0] for row in table_rows}
            columns: dict[str, set[str]] = {}
            for table, column in column_rows:
                columns.setdefault(table, set()).add(column)
        else:
            table_names = set(inspector.get_table_names(schema=schema))
            columns = {
                table: {
                    column["name"]
                    for column in inspector.get_columns(table, schema=schema)
                }
                for table in table_names
                if table in _RUNTIME_TABLE_COLUMNS or table in _RUNTIME_LEGACY_COLUMNS
            }
    except Exception as exc:
        raise RuntimeError(
            "unable to inspect Image Studio runtime schema; refusing to "
            "re-run migration DDL"
        ) from exc

    major_tables = set(_RUNTIME_TABLE_COLUMNS)
    present_major = major_tables & table_names
    present_legacy_markers = {
        table
        for table, required in _RUNTIME_LEGACY_COLUMNS.items()
        if table in columns and required & columns[table]
    }

    if not present_major and not present_legacy_markers:
        return False

    complete = present_major == major_tables and all(
        required <= columns.get(table, set())
        for table, required in _RUNTIME_TABLE_COLUMNS.items()
    )
    complete = complete and all(
        table in columns and required <= columns[table]
        for table, required in _RUNTIME_LEGACY_COLUMNS.items()
    )
    if complete:
        try:
            bind.info[_LEGACY_RUNTIME_AMBIGUITY_FLAG] = True
        except Exception:
            # Lightweight operation recorders may not expose ``info``.  The
            # migration remains safe; cleanup will simply skip reconciliation
            # when no connection-local marker can be carried forward.
            pass
        return True

    partial = sorted(present_major | present_legacy_markers)
    raise RuntimeError(
        "Image Studio runtime migration has a partial/ambiguous footprint "
        f"({', '.join(partial)}); refusing to re-run DDL automatically"
    )


_LEGACY_IMAGE_STUDIO_TABLES_REVERSE_FK = (
    "image_studio_external_sessions",
    "image_studio_cases",
    "image_studio_evaluations",
    "image_studio_outputs",
    "image_studio_references",
    "image_studio_assets",
    "image_studio_shot_revisions",
    "image_studio_shot_branches",
    "image_studio_shots",
    "image_studio_presets",
    "image_studio_workflows",
    "image_studio_concepts",
    "image_studio_characters",
    "image_studio_generations",
)


def _purge_legacy_rows_before_runtime() -> None:
    """Remove unsafe legacy rows before V2 uniqueness audits/DDL.

    The dedicated bridge immediately after this revision repeats the purge for
    all V2 tables.  This preflight is necessary because this migration itself
    adds strict legacy-table constraints (storage-path/default-workflow) and
    therefore must not inspect malformed rows that the product removal will
    discard anyway.
    """

    for table in _LEGACY_IMAGE_STUDIO_TABLES_REVERSE_FK:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                  IF to_regclass('{table}') IS NOT NULL THEN
                    EXECUTE 'DELETE FROM "{table}"';
                  END IF;
                END
                $$
                """
            )
        )


def _uuid(
    name: str,
    *,
    nullable: bool = False,
    primary_key: bool = False,
) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        primary_key=primary_key,
    )


def _json(name: str, default: str = "{}") -> sa.Column:
    return sa.Column(
        name,
        sa.JSON(),
        nullable=False,
        server_default=sa.text(f"'{default}'::json"),
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def _audit_unique_project_storage_paths() -> None:
    """Stop safely instead of deleting or silently merging legacy Asset rows."""

    op.execute(sa.text("LOCK TABLE image_studio_assets IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                  FROM image_studio_assets
                 GROUP BY project_id, storage_path
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'image_studio_assets has duplicate (project_id, storage_path); '
                  'resolve without deleting data before retrying migration';
              END IF;
            END
            $$
            """
        )
    )


def _audit_single_project_default_workflow() -> None:
    """Refuse an ambiguous default instead of choosing one by timestamp."""

    op.execute(sa.text("LOCK TABLE image_studio_workflows IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                  FROM image_studio_workflows
                 WHERE is_default = true
                 GROUP BY project_id
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'multiple default Image Studio workflows exist in one Project; '
                  'resolve explicitly before retrying migration';
              END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    if _runtime_already_applied():
        return

    _purge_legacy_rows_before_runtime()

    op.create_table(
        "image_studio_batches",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("source_shot_id", nullable=True),
        _uuid("source_revision_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        _json("render_plan_snapshot"),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("request_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_shot_id"], ["image_studio_shots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id"],
            ["image_studio_shot_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "project_id",
            "request_key_hash",
            name="uq_image_studio_batches_project_request_key",
        ),
        sa.CheckConstraint(
            "status IN "
            "('draft','queued','running','completed','partial','cancelled','failed')",
            name="ck_image_studio_batches_status",
        ),
        sa.CheckConstraint(
            "expected_run_count >= 0",
            name="ck_image_studio_batches_expected_run_count",
        ),
        sa.CheckConstraint(
            "status = 'draft' OR frozen_at IS NOT NULL",
            name="ck_image_studio_batches_non_draft_frozen",
        ),
    )
    for column in ("project_id", "source_shot_id", "source_revision_id", "created_by", "status"):
        op.create_index(f"ix_image_studio_batches_{column}", "image_studio_batches", [column])
    op.create_index(
        "ix_image_studio_batches_project_status_created",
        "image_studio_batches",
        ["project_id", "status", "created_at"],
    )

    op.create_table(
        "image_studio_execution_manifests",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("batch_id"),
        _uuid("shot_id", nullable=True),
        _uuid("branch_id", nullable=True),
        _uuid("revision_id", nullable=True),
        _uuid("workflow_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("engine", sa.String(length=32), nullable=False),
        _json("render_plan_snapshot"),
        _json("manifest_payload"),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_key", sa.String(length=160), nullable=False),
        sa.Column("workflow_version", sa.String(length=80), nullable=False),
        sa.Column("workflow_hash", sa.String(length=64), nullable=False),
        _json("api_graph_snapshot"),
        sa.Column("compiler_version", sa.String(length=80), nullable=False),
        sa.Column("compiler_hash", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["image_studio_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["image_studio_shots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["image_studio_shot_branches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["image_studio_shot_revisions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["image_studio_workflows.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "project_id", "manifest_hash", name="uq_is_manifests_project_hash"
        ),
        sa.CheckConstraint("schema_version >= 1", name="ck_is_manifests_schema_version"),
    )
    for column in (
        "project_id",
        "batch_id",
        "shot_id",
        "branch_id",
        "revision_id",
        "workflow_id",
        "created_by",
    ):
        op.create_index(
            f"ix_is_manifests_{column}",
            "image_studio_execution_manifests",
            [column],
        )
    op.create_index(
        "ix_is_manifests_project_created",
        "image_studio_execution_manifests",
        ["project_id", "created_at"],
    )

    op.create_table(
        "image_studio_runs",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("batch_id"),
        _uuid("manifest_id"),
        _uuid("generation_id", nullable=True),
        _uuid("parent_run_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("logical_job_key", sa.String(length=160), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execution_key", sa.String(length=64), nullable=False),
        sa.Column("request_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column(
            "execution_stage", sa.String(length=48), nullable=False, server_default="queued"
        ),
        sa.Column("recoverable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("fence_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("claim_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_reconcile_at", sa.DateTime(), nullable=True),
        sa.Column("engine_instance_key", sa.String(length=160), nullable=False),
        sa.Column("submission_nonce", sa.String(length=64), nullable=False),
        sa.Column("prompt_id", sa.String(length=160), nullable=True),
        sa.Column("expected_output_count", sa.Integer(), nullable=True),
        sa.Column("ready_output_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_outcome", sa.String(length=48), nullable=True),
        sa.Column("cancel_error", sa.Text(), nullable=True),
        _json("result_summary"),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["image_studio_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["image_studio_execution_manifests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"], ["image_studio_generations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["image_studio_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "project_id", "execution_key", name="uq_image_studio_runs_project_execution_key"
        ),
        sa.UniqueConstraint(
            "project_id", "request_key_hash", name="uq_image_studio_runs_project_request_key"
        ),
        sa.UniqueConstraint(
            "batch_id", "logical_job_key", name="uq_image_studio_runs_batch_logical_job"
        ),
        sa.CheckConstraint(
            "status IN "
            "('queued','running','cancel_requested','output_pending','succeeded','cancelled','failed')",
            name="ck_image_studio_runs_status",
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_image_studio_runs_attempt_no"),
        sa.CheckConstraint(
            "fence_version >= 0 AND claim_attempts >= 0 AND recovery_attempts >= 0",
            name="ck_image_studio_runs_counters",
        ),
        sa.CheckConstraint(
            "ready_output_count >= 0 AND "
            "(expected_output_count IS NULL OR "
            "(expected_output_count >= 0 AND ready_output_count <= expected_output_count))",
            name="ck_image_studio_runs_output_counts",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_image_studio_runs_lease_complete",
        ),
        sa.CheckConstraint(
            "prompt_id IS NULL OR submitted_at IS NOT NULL",
            name="ck_image_studio_runs_prompt_submitted",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','cancelled','failed') AND ended_at IS NOT NULL) "
            "OR (status NOT IN ('succeeded','cancelled','failed') AND ended_at IS NULL)",
            name="ck_image_studio_runs_terminal_ended",
        ),
        sa.CheckConstraint(
            "status <> 'cancel_requested' OR cancel_requested_at IS NOT NULL",
            name="ck_image_studio_runs_cancel_requested_at",
        ),
        sa.CheckConstraint(
            "parent_run_id IS NULL OR parent_run_id <> id",
            name="ck_image_studio_runs_no_self_parent",
        ),
    )
    for column in (
        "project_id",
        "batch_id",
        "manifest_id",
        "generation_id",
        "parent_run_id",
        "created_by",
        "status",
        "execution_stage",
        "lease_expires_at",
        "next_reconcile_at",
    ):
        op.create_index(f"ix_image_studio_runs_{column}", "image_studio_runs", [column])
    op.create_index(
        "uq_image_studio_runs_engine_prompt",
        "image_studio_runs",
        ["engine_instance_key", "prompt_id"],
        unique=True,
        postgresql_where=sa.text("prompt_id IS NOT NULL"),
    )
    op.create_index(
        "ix_image_studio_runs_project_created",
        "image_studio_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_image_studio_runs_project_status_created",
        "image_studio_runs",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_image_studio_runs_batch_status",
        "image_studio_runs",
        ["batch_id", "status"],
    )
    op.create_index(
        "ix_image_studio_runs_reconcile",
        "image_studio_runs",
        ["status", "next_reconcile_at", "lease_expires_at"],
    )

    op.create_table(
        "image_studio_run_events",
        _uuid("id", primary_key=True),
        _uuid("run_id"),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("stage", sa.String(length=48), nullable=True),
        sa.Column("fence_version", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=48), nullable=False, server_default="system"),
        _json("evidence"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["image_studio_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id", "sequence", name="uq_image_studio_run_events_sequence"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_image_studio_run_events_sequence"),
        sa.CheckConstraint(
            "fence_version IS NULL OR fence_version >= 0",
            name="ck_image_studio_run_events_fence",
        ),
    )
    op.create_index("ix_image_studio_run_events_run_id", "image_studio_run_events", ["run_id"])
    op.create_index(
        "ix_image_studio_run_events_event_type",
        "image_studio_run_events",
        ["event_type"],
    )
    op.create_index(
        "ix_image_studio_run_events_created_at",
        "image_studio_run_events",
        ["created_at"],
    )
    op.create_index(
        "ix_image_studio_run_events_run_created",
        "image_studio_run_events",
        ["run_id", "created_at"],
    )

    op.alter_column(
        "image_studio_outputs",
        "generation_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_constraint(
        "image_studio_outputs_generation_id_fkey",
        "image_studio_outputs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_image_studio_outputs_generation_id",
        "image_studio_outputs",
        "image_studio_generations",
        ["generation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("image_studio_outputs", _uuid("run_id", nullable=True))
    op.add_column(
        "image_studio_outputs",
        sa.Column("engine_output_key", sa.String(length=256), nullable=True),
    )
    op.add_column("image_studio_outputs", _json("engine_locator"))
    op.add_column(
        "image_studio_outputs",
        sa.Column("ingest_state", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "image_studio_outputs",
        sa.Column("ingest_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("image_studio_outputs", sa.Column("ingest_error", sa.Text(), nullable=True))
    op.add_column(
        "image_studio_outputs", sa.Column("last_ingest_at", sa.DateTime(), nullable=True)
    )
    op.create_foreign_key(
        "fk_image_studio_outputs_run_id",
        "image_studio_outputs",
        "image_studio_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute(
        sa.text(
            """
            UPDATE image_studio_outputs
               SET ingest_state = CASE WHEN asset_id IS NOT NULL THEN 'ready' ELSE 'pending' END
            """
        )
    )
    op.create_check_constraint(
        "ck_image_studio_outputs_ingest_state",
        "image_studio_outputs",
        "ingest_state IN ('pending','ingesting','ready','failed','quarantined')",
    )
    op.create_check_constraint(
        "ck_image_studio_outputs_ingest_attempts",
        "image_studio_outputs",
        "ingest_attempts >= 0",
    )
    op.create_check_constraint(
        "ck_image_studio_outputs_ready_asset",
        "image_studio_outputs",
        "ingest_state <> 'ready' OR asset_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_image_studio_outputs_execution_owner",
        "image_studio_outputs",
        "run_id IS NOT NULL OR generation_id IS NOT NULL",
    )
    op.create_index("ix_image_studio_outputs_run_id", "image_studio_outputs", ["run_id"])
    op.create_index(
        "ix_image_studio_outputs_ingest_state",
        "image_studio_outputs",
        ["ingest_state"],
    )
    op.create_index(
        "ix_image_studio_outputs_run_ingest",
        "image_studio_outputs",
        ["run_id", "ingest_state"],
    )
    op.create_index(
        "uq_image_studio_outputs_run_variant",
        "image_studio_outputs",
        ["run_id", "variant_index"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index(
        "uq_image_studio_outputs_run_engine_key",
        "image_studio_outputs",
        ["run_id", "engine_output_key"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL AND engine_output_key IS NOT NULL"),
    )

    op.add_column(
        "image_studio_assets",
        sa.Column(
            "storage_state",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_unverified",
        ),
    )
    op.add_column("image_studio_assets", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("image_studio_assets", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("image_studio_assets", _uuid("thumbnail_asset_id", nullable=True))
    op.add_column(
        "image_studio_assets", sa.Column("delivery_key", sa.String(length=128), nullable=True)
    )
    op.create_foreign_key(
        "fk_image_studio_assets_thumbnail_asset_id",
        "image_studio_assets",
        "image_studio_assets",
        ["thumbnail_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_image_studio_assets_storage_state",
        "image_studio_assets",
        "storage_state IN "
        "('pending','ready','missing','deleting','delete_failed','trashed','legacy_unverified')",
    )
    op.create_check_constraint(
        "ck_image_studio_assets_width",
        "image_studio_assets",
        "width IS NULL OR width > 0",
    )
    op.create_check_constraint(
        "ck_image_studio_assets_height",
        "image_studio_assets",
        "height IS NULL OR height > 0",
    )
    op.create_check_constraint(
        "ck_image_studio_assets_no_self_thumbnail",
        "image_studio_assets",
        "thumbnail_asset_id IS NULL OR thumbnail_asset_id <> id",
    )
    op.alter_column(
        "image_studio_assets",
        "storage_state",
        existing_type=sa.String(length=32),
        server_default="pending",
        existing_nullable=False,
    )
    _audit_unique_project_storage_paths()
    op.create_unique_constraint(
        "uq_image_studio_assets_project_storage_path",
        "image_studio_assets",
        ["project_id", "storage_path"],
    )
    op.create_index(
        "ix_image_studio_assets_storage_state",
        "image_studio_assets",
        ["storage_state"],
    )
    op.create_index(
        "ix_image_studio_assets_thumbnail_asset_id",
        "image_studio_assets",
        ["thumbnail_asset_id"],
    )
    op.create_index(
        "uq_image_studio_assets_project_delivery_key",
        "image_studio_assets",
        ["project_id", "delivery_key"],
        unique=True,
        postgresql_where=sa.text("delivery_key IS NOT NULL"),
    )
    op.create_index(
        "ix_image_studio_assets_project_checksum",
        "image_studio_assets",
        ["project_id", "checksum_sha256"],
    )

    op.add_column(
        "image_studio_workflows", sa.Column("frozen_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "image_studio_workflows", _uuid("supersedes_workflow_id", nullable=True)
    )
    op.create_foreign_key(
        "fk_image_studio_workflows_supersedes",
        "image_studio_workflows",
        "image_studio_workflows",
        ["supersedes_workflow_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        sa.text(
            """
            UPDATE image_studio_workflows
               SET frozen_at = COALESCE(updated_at, created_at)
             WHERE frozen_at IS NULL
            """
        )
    )
    op.create_check_constraint(
        "ck_image_studio_workflows_no_self_supersedes",
        "image_studio_workflows",
        "supersedes_workflow_id IS NULL OR supersedes_workflow_id <> id",
    )
    _audit_single_project_default_workflow()
    op.create_index(
        "uq_image_studio_workflows_project_default",
        "image_studio_workflows",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )
    op.create_index(
        "ix_image_studio_workflows_supersedes_workflow_id",
        "image_studio_workflows",
        ["supersedes_workflow_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_workflows_supersedes_workflow_id",
        table_name="image_studio_workflows",
    )
    op.drop_index(
        "uq_image_studio_workflows_project_default",
        table_name="image_studio_workflows",
    )
    op.drop_constraint(
        "ck_image_studio_workflows_no_self_supersedes",
        "image_studio_workflows",
        type_="check",
    )
    op.drop_constraint(
        "fk_image_studio_workflows_supersedes",
        "image_studio_workflows",
        type_="foreignkey",
    )
    op.drop_column("image_studio_workflows", "supersedes_workflow_id")
    op.drop_column("image_studio_workflows", "frozen_at")

    op.drop_index(
        "uq_image_studio_assets_project_delivery_key",
        table_name="image_studio_assets",
    )
    op.drop_index(
        "ix_image_studio_assets_project_checksum",
        table_name="image_studio_assets",
    )
    op.drop_index(
        "ix_image_studio_assets_thumbnail_asset_id",
        table_name="image_studio_assets",
    )
    op.drop_index("ix_image_studio_assets_storage_state", table_name="image_studio_assets")
    op.drop_constraint(
        "uq_image_studio_assets_project_storage_path",
        "image_studio_assets",
        type_="unique",
    )
    for name in (
        "ck_image_studio_assets_no_self_thumbnail",
        "ck_image_studio_assets_height",
        "ck_image_studio_assets_width",
        "ck_image_studio_assets_storage_state",
    ):
        op.drop_constraint(name, "image_studio_assets", type_="check")
    op.drop_constraint(
        "fk_image_studio_assets_thumbnail_asset_id",
        "image_studio_assets",
        type_="foreignkey",
    )
    for column in ("delivery_key", "thumbnail_asset_id", "height", "width", "storage_state"):
        op.drop_column("image_studio_assets", column)

    op.drop_index(
        "uq_image_studio_outputs_run_engine_key",
        table_name="image_studio_outputs",
    )
    op.drop_index("ix_image_studio_outputs_run_ingest", table_name="image_studio_outputs")
    op.drop_index("uq_image_studio_outputs_run_variant", table_name="image_studio_outputs")
    op.drop_index("ix_image_studio_outputs_ingest_state", table_name="image_studio_outputs")
    op.drop_index("ix_image_studio_outputs_run_id", table_name="image_studio_outputs")
    for name in (
        "ck_image_studio_outputs_execution_owner",
        "ck_image_studio_outputs_ready_asset",
        "ck_image_studio_outputs_ingest_attempts",
        "ck_image_studio_outputs_ingest_state",
    ):
        op.drop_constraint(name, "image_studio_outputs", type_="check")
    op.drop_constraint(
        "fk_image_studio_outputs_run_id",
        "image_studio_outputs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_image_studio_outputs_generation_id",
        "image_studio_outputs",
        type_="foreignkey",
    )
    for column in (
        "last_ingest_at",
        "ingest_error",
        "ingest_attempts",
        "ingest_state",
        "engine_locator",
        "engine_output_key",
        "run_id",
    ):
        op.drop_column("image_studio_outputs", column)
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM image_studio_outputs WHERE generation_id IS NULL
              ) THEN
                RAISE EXCEPTION
                  'cannot downgrade: Image Studio outputs without generation_id exist';
              END IF;
            END
            $$
            """
        )
    )
    op.alter_column(
        "image_studio_outputs",
        "generation_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "image_studio_outputs_generation_id_fkey",
        "image_studio_outputs",
        "image_studio_generations",
        ["generation_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_index(
        "ix_image_studio_run_events_run_created",
        table_name="image_studio_run_events",
    )
    op.drop_index("ix_image_studio_run_events_created_at", table_name="image_studio_run_events")
    op.drop_index("ix_image_studio_run_events_event_type", table_name="image_studio_run_events")
    op.drop_index("ix_image_studio_run_events_run_id", table_name="image_studio_run_events")
    op.drop_table("image_studio_run_events")

    op.drop_index("ix_image_studio_runs_reconcile", table_name="image_studio_runs")
    op.drop_index("ix_image_studio_runs_batch_status", table_name="image_studio_runs")
    op.drop_index(
        "ix_image_studio_runs_project_status_created",
        table_name="image_studio_runs",
    )
    op.drop_index("ix_image_studio_runs_project_created", table_name="image_studio_runs")
    op.drop_index("uq_image_studio_runs_engine_prompt", table_name="image_studio_runs")
    for column in (
        "next_reconcile_at",
        "lease_expires_at",
        "execution_stage",
        "status",
        "created_by",
        "parent_run_id",
        "generation_id",
        "manifest_id",
        "batch_id",
        "project_id",
    ):
        op.drop_index(f"ix_image_studio_runs_{column}", table_name="image_studio_runs")
    op.drop_table("image_studio_runs")

    op.drop_index(
        "ix_is_manifests_project_created",
        table_name="image_studio_execution_manifests",
    )
    for column in (
        "created_by",
        "workflow_id",
        "revision_id",
        "branch_id",
        "shot_id",
        "batch_id",
        "project_id",
    ):
        op.drop_index(
            f"ix_is_manifests_{column}",
            table_name="image_studio_execution_manifests",
        )
    op.drop_table("image_studio_execution_manifests")

    op.drop_index(
        "ix_image_studio_batches_project_status_created",
        table_name="image_studio_batches",
    )
    for column in ("status", "created_by", "source_revision_id", "source_shot_id", "project_id"):
        op.drop_index(f"ix_image_studio_batches_{column}", table_name="image_studio_batches")
    op.drop_table("image_studio_batches")
