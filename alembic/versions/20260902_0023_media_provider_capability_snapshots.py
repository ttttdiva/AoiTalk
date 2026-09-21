"""Add immutable, secret-free provider capability snapshots.

The code-owned provider policy is versioned in the application registry.  This
table stores only a redacted observation bound to an account, account revision
and credential revision.  It deliberately has no token, cookie, ciphertext,
provider response or filesystem path field.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260902_0023"
down_revision = "20260902_0022"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "media_provider_capability_snapshots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("platform_account_id", _uuid(), nullable=False),
        sa.Column("account_revision_id", _uuid(), nullable=False),
        sa.Column("credential_id", _uuid(), nullable=False),
        sa.Column("credential_state_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("account_revision", sa.Integer(), nullable=False),
        sa.Column("credential_scope", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("registry_version", sa.String(length=32), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("account_eligibility", sa.String(length=16), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("adapter_key", sa.String(length=128), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('x', 'pixiv', 'patreon', 'youtube', 'instagram', 'dlsite')",
            name="ck_media_provider_capability_snapshots_provider",
        ),
        sa.CheckConstraint(
            "operation IN ('identity', 'oauth', 'token', 'cookie', 'text', 'image', 'video', 'schedule', 'edit', 'delete', 'analytics', 'revenue', 'refresh', 'revoke')",
            name="ck_media_provider_capability_snapshots_operation",
        ),
        sa.CheckConstraint(
            "status IN ('automatable', 'manual', 'unsupported', 'unverified', 'unavailable')",
            name="ck_media_provider_capability_snapshots_status",
        ),
        sa.CheckConstraint(
            "account_eligibility IN ('unknown', 'eligible', 'ineligible', 'unverified')",
            name="ck_media_provider_capability_snapshots_account_eligibility",
        ),
        sa.CheckConstraint(
            "credential_revision > 0",
            name="ck_media_provider_capability_snapshots_credential_revision",
        ),
        sa.CheckConstraint(
            "account_revision > 0",
            name="ck_media_provider_capability_snapshots_account_revision",
        ),
        sa.CheckConstraint(
            "length(credential_state_hash) = 64",
            name="ck_media_provider_capability_snapshots_credential_state_hash",
        ),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_media_provider_capability_snapshots_snapshot_hash",
        ),
        sa.CheckConstraint(
            "length(trim(credential_scope)) > 0",
            name="ck_media_provider_capability_snapshots_credential_scope",
        ),
        sa.CheckConstraint(
            "length(trim(adapter_key)) > 0",
            name="ck_media_provider_capability_snapshots_adapter_key",
        ),
        sa.CheckConstraint(
            "length(trim(adapter_version)) > 0",
            name="ck_media_provider_capability_snapshots_adapter_version",
        ),
        sa.CheckConstraint(
            "length(trim(registry_version)) > 0",
            name="ck_media_provider_capability_snapshots_registry_version",
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
            ["platform_account_id"],
            ["media_platform_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_revision_id"],
            ["media_platform_account_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["media_platform_credentials.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform_account_id",
            "operation",
            "credential_revision",
            "account_revision",
            "snapshot_hash",
            name="uq_media_provider_capability_snapshots_observation",
        ),
    )
    for name, columns in {
        "ix_media_provider_capability_snapshots_owner_user_id": ["owner_user_id"],
        "ix_media_provider_capability_snapshots_project_id": ["project_id"],
        "ix_media_provider_capability_snapshots_platform_account_id": ["platform_account_id"],
        "ix_media_provider_capability_snapshots_account_revision_id": ["account_revision_id"],
        "ix_media_provider_capability_snapshots_credential_id": ["credential_id"],
        "ix_media_provider_capability_snapshots_snapshot_hash": ["snapshot_hash"],
        "ix_media_provider_capability_snapshots_observed_at": ["observed_at"],
        "ix_media_provider_capability_snapshots_owner_project": ["owner_user_id", "project_id"],
        "ix_media_provider_cap_snap_account_op_observed": [
            "platform_account_id",
            "operation",
            "observed_at",
        ],
    }.items():
        op.create_index(name, "media_provider_capability_snapshots", columns)
    op.create_index(
        "uq_media_provider_capability_snapshots_personal_idempotency",
        "media_provider_capability_snapshots",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_provider_capability_snapshots_project_idempotency",
        "media_provider_capability_snapshots",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION media_provider_capability_snapshots_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'media provider capability snapshots are immutable';
            END;
            $$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_provider_capability_snapshots_no_update
            BEFORE UPDATE OR DELETE ON media_provider_capability_snapshots
            FOR EACH ROW EXECUTE FUNCTION media_provider_capability_snapshots_immutable();
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_provider_capability_snapshots_no_truncate
            BEFORE TRUNCATE ON media_provider_capability_snapshots
            FOR EACH STATEMENT EXECUTE FUNCTION media_provider_capability_snapshots_immutable();
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER media_provider_capability_snapshots_no_update
            BEFORE UPDATE ON media_provider_capability_snapshots
            BEGIN
              SELECT RAISE(ABORT, 'media provider capability snapshots are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_provider_capability_snapshots_no_delete
            BEFORE DELETE ON media_provider_capability_snapshots
            BEGIN
              SELECT RAISE(ABORT, 'media provider capability snapshots are immutable');
            END;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS media_provider_capability_snapshots_no_update "
            "ON media_provider_capability_snapshots"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS media_provider_capability_snapshots_no_truncate "
            "ON media_provider_capability_snapshots"
        )
        op.execute("DROP FUNCTION IF EXISTS media_provider_capability_snapshots_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS media_provider_capability_snapshots_no_update")
        op.execute("DROP TRIGGER IF EXISTS media_provider_capability_snapshots_no_delete")
    op.drop_table("media_provider_capability_snapshots")
