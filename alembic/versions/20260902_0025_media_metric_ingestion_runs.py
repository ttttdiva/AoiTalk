"""Add the append-only provider metrics ingestion ledger.

The provider adapters are deliberately kept outside the database boundary.
This table records only a bounded checkpoint and redacted evidence tuple so a
retry can be correlated without retaining provider responses, credentials or
filesystem paths.  A repeated idempotency key is one immutable replay; a new
fetch/correction must append a new key.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0025"
down_revision = "20260902_0024"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _install_immutable_guard() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION media_metric_ingestion_runs_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'media metric ingestion runs are immutable';
            END;
            $$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_metric_ingestion_runs_no_update
            BEFORE UPDATE OR DELETE ON media_metric_ingestion_runs
            FOR EACH ROW EXECUTE FUNCTION media_metric_ingestion_runs_immutable();
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_metric_ingestion_runs_no_truncate
            BEFORE TRUNCATE ON media_metric_ingestion_runs
            FOR EACH STATEMENT EXECUTE FUNCTION media_metric_ingestion_runs_immutable();
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER media_metric_ingestion_runs_no_update
            BEFORE UPDATE ON media_metric_ingestion_runs
            BEGIN
              SELECT RAISE(ABORT, 'media metric ingestion runs are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_metric_ingestion_runs_no_delete
            BEFORE DELETE ON media_metric_ingestion_runs
            BEGIN
              SELECT RAISE(ABORT, 'media metric ingestion runs are immutable');
            END;
            """
        )


def upgrade() -> None:
    op.create_table(
        "media_metric_ingestion_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("platform_account_id", _uuid(), nullable=True),
        sa.Column("platform_account_ref", sa.String(length=164), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("window_end", sa.DateTime(), nullable=True),
        sa.Column("cursor", sa.String(length=512), nullable=True),
        sa.Column("checkpoint", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("platform_account_revision_id", _uuid(), nullable=True),
        sa.Column("platform_account_revision_hash", sa.String(length=64), nullable=True),
        sa.Column("credential_state_hash", sa.String(length=64), nullable=True),
        sa.Column("capability_snapshot_id", _uuid(), nullable=True),
        sa.Column("capability_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("external_action_receipt_ref", sa.String(length=164), nullable=True),
        sa.Column("remote_ref", sa.String(length=164), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["platform_account_id"], ["media_platform_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["platform_account_revision_id"], ["media_platform_account_revisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["capability_snapshot_id"], ["media_provider_capability_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "provider IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_metric_ingestion_runs_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'uncertain')",
            name="ck_media_metric_ingestion_runs_status",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_media_metric_ingestion_runs_request_hash",
        ),
        sa.CheckConstraint(
            "length(observation_hash) = 64",
            name="ck_media_metric_ingestion_runs_observation_hash",
        ),
        sa.CheckConstraint(
            "platform_account_revision_hash IS NULL OR length(platform_account_revision_hash) = 64",
            name="ck_media_metric_ingestion_runs_account_revision_hash",
        ),
        sa.CheckConstraint(
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
            name="ck_media_metric_ingestion_runs_credential_state_hash",
        ),
        sa.CheckConstraint(
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
            name="ck_media_metric_ingestion_runs_capability_snapshot_hash",
        ),
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_owner_user_id",
        "media_metric_ingestion_runs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_project_id",
        "media_metric_ingestion_runs",
        ["project_id"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_provider",
        "media_metric_ingestion_runs",
        ["provider"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_platform_account_id",
        "media_metric_ingestion_runs",
        ["platform_account_id"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_platform_account_ref",
        "media_metric_ingestion_runs",
        ["platform_account_ref"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_window_start",
        "media_metric_ingestion_runs",
        ["window_start"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_window_end",
        "media_metric_ingestion_runs",
        ["window_end"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_status",
        "media_metric_ingestion_runs",
        ["status"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_request_hash",
        "media_metric_ingestion_runs",
        ["request_hash"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_observation_hash",
        "media_metric_ingestion_runs",
        ["observation_hash"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_platform_account_revision_id",
        "media_metric_ingestion_runs",
        ["platform_account_revision_id"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_capability_snapshot_id",
        "media_metric_ingestion_runs",
        ["capability_snapshot_id"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_remote_ref",
        "media_metric_ingestion_runs",
        ["remote_ref"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_created_at",
        "media_metric_ingestion_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_media_metric_ingestion_runs_owner_project",
        "media_metric_ingestion_runs",
        ["owner_user_id", "project_id"],
    )
    # Partial unique indexes preserve separate personal and project scopes.
    op.create_index(
        "uq_media_metric_ingestion_runs_personal_idempotency",
        "media_metric_ingestion_runs",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_metric_ingestion_runs_project_idempotency",
        "media_metric_ingestion_runs",
        ["project_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("project_id IS NOT NULL"),
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    _install_immutable_guard()


def downgrade() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS media_metric_ingestion_runs_no_update "
            "ON media_metric_ingestion_runs"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS media_metric_ingestion_runs_no_truncate "
            "ON media_metric_ingestion_runs"
        )
        op.execute("DROP FUNCTION IF EXISTS media_metric_ingestion_runs_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS media_metric_ingestion_runs_no_update")
        op.execute("DROP TRIGGER IF EXISTS media_metric_ingestion_runs_no_delete")
    for name in (
        "uq_media_metric_ingestion_runs_project_idempotency",
        "uq_media_metric_ingestion_runs_personal_idempotency",
        "ix_media_metric_ingestion_runs_owner_project",
        "ix_media_metric_ingestion_runs_created_at",
        "ix_media_metric_ingestion_runs_remote_ref",
        "ix_media_metric_ingestion_runs_capability_snapshot_id",
        "ix_media_metric_ingestion_runs_platform_account_revision_id",
        "ix_media_metric_ingestion_runs_observation_hash",
        "ix_media_metric_ingestion_runs_request_hash",
        "ix_media_metric_ingestion_runs_status",
        "ix_media_metric_ingestion_runs_window_end",
        "ix_media_metric_ingestion_runs_window_start",
        "ix_media_metric_ingestion_runs_platform_account_ref",
        "ix_media_metric_ingestion_runs_platform_account_id",
        "ix_media_metric_ingestion_runs_provider",
        "ix_media_metric_ingestion_runs_project_id",
        "ix_media_metric_ingestion_runs_owner_user_id",
    ):
        op.drop_index(name, table_name="media_metric_ingestion_runs")
    op.drop_table("media_metric_ingestion_runs")
