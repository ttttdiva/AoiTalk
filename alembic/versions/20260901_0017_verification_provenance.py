"""Add the server-owned verification provenance and cleanup ledger.

The ledger is an explicit allow-list for disposable verification artifacts.
No user-facing title, lifecycle state, age, or free-form request metadata is
used as deletion authority.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0017"
down_revision = "20260901_0016"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    # Alembic's offline mode supplies a ``MockConnection`` without an
    # inspection implementation.  Treat that as an empty schema so the
    # generated SQL still contains the complete forward migration; online
    # runs retain the idempotent inspection guard.
    try:
        return table_name in sa.inspect(bind).get_table_names()
    except sa.exc.NoInspectionAvailable:
        return False


def _indexes(bind: sa.Connection, table_name: str) -> set[str]:
    if not _table_exists(bind, table_name):
        return set()
    try:
        return {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}
    except sa.exc.NoInspectionAvailable:
        return set()


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _indexes(op.get_bind(), table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "verification_runs"):
        op.create_table(
            "verification_runs",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("run_id", _uuid(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("disposable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("source", sa.String(length=255), nullable=False),
            sa.Column("harness", sa.String(length=255), nullable=True),
            sa.Column("actor_user_id", _uuid(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'running'")),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "schema_version > 0",
                name="ck_verification_runs_schema_version_positive",
            ),
            sa.CheckConstraint(
                "disposable = true",
                name="ck_verification_runs_disposable",
            ),
            sa.CheckConstraint(
                "length(source) > 0",
                name="ck_verification_runs_source_nonempty",
            ),
            sa.CheckConstraint(
                "status IN ('running','succeeded','failed','aborted','cleaned','cleanup_failed')",
                name="ck_verification_runs_status",
            ),
            sa.ForeignKeyConstraint(
                ["actor_user_id"],
                ["users.id"],
                name="fk_verification_runs_actor_user_id",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", name="uq_verification_runs_run_id"),
        )
    _create_index_if_missing("ix_verification_runs_run_id", "verification_runs", ["run_id"])
    _create_index_if_missing("ix_verification_runs_actor_user_id", "verification_runs", ["actor_user_id"])
    _create_index_if_missing("ix_verification_runs_status", "verification_runs", ["status"])
    _create_index_if_missing("ix_verification_runs_created_at", "verification_runs", ["created_at"])
    _create_index_if_missing(
        "ix_verification_runs_disposable_status_created",
        "verification_runs",
        ["disposable", "status", "created_at"],
    )

    if not _table_exists(bind, "verification_artifact_provenance"):
        op.create_table(
            "verification_artifact_provenance",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("run_id", _uuid(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("disposable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("entity_type", sa.String(length=64), nullable=False),
            sa.Column("entity_id", sa.String(length=512), nullable=False),
            sa.Column("source", sa.String(length=255), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("cleanup_status", sa.String(length=24), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("cleanup_error_code", sa.String(length=96), nullable=True),
            sa.Column("cleaned_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "schema_version > 0",
                name="ck_verification_artifact_schema_version_positive",
            ),
            sa.CheckConstraint(
                "disposable = true",
                name="ck_verification_artifact_disposable",
            ),
            sa.CheckConstraint(
                "length(entity_type) > 0",
                name="ck_verification_artifact_entity_type_nonempty",
            ),
            sa.CheckConstraint(
                "length(entity_id) > 0",
                name="ck_verification_artifact_entity_id_nonempty",
            ),
            sa.CheckConstraint(
                "length(source) > 0",
                name="ck_verification_artifact_source_nonempty",
            ),
            sa.CheckConstraint(
                "cleanup_status IN ('pending','deleted','already_absent','failed','skipped')",
                name="ck_verification_artifact_cleanup_status",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["verification_runs.run_id"],
                name="fk_verification_artifact_run_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "run_id",
                "entity_type",
                "entity_id",
                name="uq_verification_artifact_provenance_identity",
            ),
        )
    _create_index_if_missing("ix_verification_artifact_provenance_run_id", "verification_artifact_provenance", ["run_id"])
    _create_index_if_missing("ix_verification_artifact_provenance_cleanup_status", "verification_artifact_provenance", ["cleanup_status"])
    _create_index_if_missing("ix_verification_artifact_provenance_created_at", "verification_artifact_provenance", ["created_at"])
    _create_index_if_missing(
        "ix_verification_artifact_run_type_status",
        "verification_artifact_provenance",
        ["run_id", "entity_type", "cleanup_status"],
    )

    if not _table_exists(bind, "verification_cleanup_runs"):
        op.create_table(
            "verification_cleanup_runs",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("run_id", _uuid(), nullable=False),
            sa.Column("actor_user_id", _uuid(), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default=sa.text("'running'")),
            sa.Column("confirmation_sha256", sa.String(length=64), nullable=True),
            sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('running','succeeded','failed','partial','already_clean')",
                name="ck_verification_cleanup_runs_status",
            ),
            sa.CheckConstraint(
                "confirmation_sha256 IS NULL OR length(confirmation_sha256) = 64",
                name="ck_verification_cleanup_runs_confirmation_sha256",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["verification_runs.run_id"],
                name="fk_verification_cleanup_runs_run_id",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["actor_user_id"],
                ["users.id"],
                name="fk_verification_cleanup_runs_actor_user_id",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_if_missing("ix_verification_cleanup_runs_run_id", "verification_cleanup_runs", ["run_id"])
    _create_index_if_missing("ix_verification_cleanup_runs_actor_user_id", "verification_cleanup_runs", ["actor_user_id"])
    _create_index_if_missing("ix_verification_cleanup_runs_status", "verification_cleanup_runs", ["status"])
    _create_index_if_missing("ix_verification_cleanup_runs_created_at", "verification_cleanup_runs", ["created_at"])

    if not _table_exists(bind, "verification_cleanup_items"):
        op.create_table(
            "verification_cleanup_items",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("cleanup_run_id", _uuid(), nullable=False),
            sa.Column("artifact_id", _uuid(), nullable=True),
            sa.Column("run_id", _uuid(), nullable=False),
            sa.Column("entity_type", sa.String(length=64), nullable=False),
            sa.Column("entity_id", sa.String(length=512), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("safe_error_code", sa.String(length=96), nullable=True),
            sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending','deleted','already_absent','failed','skipped')",
                name="ck_verification_cleanup_items_status",
            ),
            sa.ForeignKeyConstraint(
                ["cleanup_run_id"],
                ["verification_cleanup_runs.id"],
                name="fk_verification_cleanup_items_cleanup_run_id",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["artifact_id"],
                ["verification_artifact_provenance.id"],
                name="fk_verification_cleanup_items_artifact_id",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["verification_runs.run_id"],
                name="fk_verification_cleanup_items_run_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "cleanup_run_id",
                "entity_type",
                "entity_id",
                name="uq_verification_cleanup_item_identity",
            ),
        )
    _create_index_if_missing("ix_verification_cleanup_items_cleanup_run_id", "verification_cleanup_items", ["cleanup_run_id"])
    _create_index_if_missing("ix_verification_cleanup_items_artifact_id", "verification_cleanup_items", ["artifact_id"])
    _create_index_if_missing("ix_verification_cleanup_items_run_id", "verification_cleanup_items", ["run_id"])
    _create_index_if_missing("ix_verification_cleanup_items_status", "verification_cleanup_items", ["status"])
    _create_index_if_missing("ix_verification_cleanup_items_created_at", "verification_cleanup_items", ["created_at"])
    _create_index_if_missing(
        "ix_verification_cleanup_items_run_status",
        "verification_cleanup_items",
        ["run_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Drop children before parents so PostgreSQL and SQLite agree even when
    # foreign-key enforcement is enabled in an offline test database.
    # Offline Alembic uses a ``MockConnection`` that cannot be inspected; in
    # that mode emit unconditional DROP statements so a generated downgrade
    # script remains useful.  Online runs retain the idempotent existence
    # guard to tolerate partially-applied migrations.
    try:
        inspection_available = sa.inspect(bind)
    except sa.exc.NoInspectionAvailable:
        inspection_available = None
    for table in (
        "verification_cleanup_items",
        "verification_cleanup_runs",
        "verification_artifact_provenance",
        "verification_runs",
    ):
        if inspection_available is None or _table_exists(bind, table):
            op.drop_table(table)
