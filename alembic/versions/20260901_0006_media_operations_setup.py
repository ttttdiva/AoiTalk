"""Create MediaOps bulk Persona drafts and Platform Accounts."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0006"
down_revision = "20260901_0005"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "media_persona_bulk_drafts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'1'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "apply_idempotency_key",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "apply_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_media_persona_bulk_drafts_source_hash",
        ),
        sa.CheckConstraint(
            "length(draft_hash) = 64",
            name="ck_media_persona_bulk_drafts_draft_hash",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_media_persona_bulk_drafts_version_positive",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'applied')",
            name="ck_media_persona_bulk_drafts_status",
        ),
        sa.CheckConstraint(
            "("
            "status = 'draft' "
            "AND apply_idempotency_key IS NULL "
            "AND apply_hash IS NULL "
            "AND applied_at IS NULL"
            ") OR ("
            "status = 'applied' "
            "AND apply_idempotency_key IS NOT NULL "
            "AND apply_hash IS NOT NULL "
            "AND applied_at IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_drafts_apply_shape",
        ),
        sa.CheckConstraint(
            "apply_hash IS NULL OR length(apply_hash) = 64",
            name="ck_media_persona_bulk_drafts_apply_hash",
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
    )

    op.create_index(
        "ix_media_persona_bulk_drafts_owner_user_id",
        "media_persona_bulk_drafts",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_project_id",
        "media_persona_bulk_drafts",
        ["project_id"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_source_hash",
        "media_persona_bulk_drafts",
        ["source_hash"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_draft_hash",
        "media_persona_bulk_drafts",
        ["draft_hash"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_status",
        "media_persona_bulk_drafts",
        ["status"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_created_at",
        "media_persona_bulk_drafts",
        ["created_at"],
    )
    op.create_index(
        "ix_media_persona_bulk_drafts_owner_project",
        "media_persona_bulk_drafts",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_persona_bulk_drafts_personal_idempotency",
        "media_persona_bulk_drafts",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_persona_bulk_drafts_project_idempotency",
        "media_persona_bulk_drafts",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "media_persona_bulk_draft_slots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("draft_id", _uuid(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column(
            "display_name_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "display_name_value",
            sa.String(length=120),
            nullable=True,
        ),
        sa.Column(
            "display_name_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "summary_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "summary_value",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "summary_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "voice_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "voice_value",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "voice_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "audience_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "audience_value",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "audience_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "platforms_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "platform_x",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platform_pixiv",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platform_dlsite",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platform_patreon",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platform_youtube",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platform_instagram",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "platforms_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "content_pillars_state",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "content_pillars",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "content_pillars_evidence",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "slot_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slot >= 1 AND slot <= 9",
            name="ck_media_persona_bulk_draft_slots_range",
        ),
        sa.CheckConstraint(
            "length(slot_hash) = 64",
            name="ck_media_persona_bulk_draft_slots_hash",
        ),
        sa.CheckConstraint(
            "("
            "display_name_state = 'unknown' "
            "AND display_name_value IS NULL "
            "AND display_name_evidence IS NULL"
            ") OR ("
            "display_name_state = 'explicit' "
            "AND display_name_value IS NOT NULL"
            ") OR ("
            "display_name_state = 'inferred' "
            "AND display_name_value IS NOT NULL "
            "AND display_name_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_display_name_fact",
        ),
        sa.CheckConstraint(
            "("
            "summary_state = 'unknown' "
            "AND summary_value IS NULL "
            "AND summary_evidence IS NULL"
            ") OR ("
            "summary_state = 'explicit' "
            "AND summary_value IS NOT NULL"
            ") OR ("
            "summary_state = 'inferred' "
            "AND summary_value IS NOT NULL "
            "AND summary_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_summary_fact",
        ),
        sa.CheckConstraint(
            "("
            "voice_state = 'unknown' "
            "AND voice_value IS NULL "
            "AND voice_evidence IS NULL"
            ") OR ("
            "voice_state = 'explicit' "
            "AND voice_value IS NOT NULL"
            ") OR ("
            "voice_state = 'inferred' "
            "AND voice_value IS NOT NULL "
            "AND voice_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_voice_fact",
        ),
        sa.CheckConstraint(
            "("
            "audience_state = 'unknown' "
            "AND audience_value IS NULL "
            "AND audience_evidence IS NULL"
            ") OR ("
            "audience_state = 'explicit' "
            "AND audience_value IS NOT NULL"
            ") OR ("
            "audience_state = 'inferred' "
            "AND audience_value IS NOT NULL "
            "AND audience_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_audience_fact",
        ),
        sa.CheckConstraint(
            "("
            "platforms_state = 'unknown' "
            "AND platform_x IS NULL "
            "AND platform_pixiv IS NULL "
            "AND platform_dlsite IS NULL "
            "AND platform_patreon IS NULL "
            "AND platform_youtube IS NULL "
            "AND platform_instagram IS NULL "
            "AND platforms_evidence IS NULL"
            ") OR ("
            "platforms_state = 'explicit' "
            "AND platform_x IS NOT NULL "
            "AND platform_pixiv IS NOT NULL "
            "AND platform_dlsite IS NOT NULL "
            "AND platform_patreon IS NOT NULL "
            "AND platform_youtube IS NOT NULL "
            "AND platform_instagram IS NOT NULL"
            ") OR ("
            "platforms_state = 'inferred' "
            "AND platform_x IS NOT NULL "
            "AND platform_pixiv IS NOT NULL "
            "AND platform_dlsite IS NOT NULL "
            "AND platform_patreon IS NOT NULL "
            "AND platform_youtube IS NOT NULL "
            "AND platform_instagram IS NOT NULL "
            "AND platforms_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_platforms_fact",
        ),
        sa.CheckConstraint(
            "content_pillars_state IN "
            "('explicit', 'inferred', 'unknown')",
            name="ck_media_persona_bulk_draft_pillars_state",
        ),
        sa.CheckConstraint(
            "content_pillars_state != 'inferred' "
            "OR content_pillars_evidence IS NOT NULL",
            name="ck_media_persona_bulk_draft_pillars_inferred_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["media_persona_bulk_drafts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "draft_id",
            "slot",
            name="uq_media_persona_bulk_draft_slots_slot",
        ),
    )
    op.create_index(
        "ix_media_persona_bulk_draft_slots_draft_id",
        "media_persona_bulk_draft_slots",
        ["draft_id"],
    )
    op.create_index(
        "ix_media_persona_bulk_draft_slots_slot_hash",
        "media_persona_bulk_draft_slots",
        ["slot_hash"],
    )

    op.create_table(
        "media_platform_accounts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "account_ref",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "create_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "platform IN "
            "('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_platform_accounts_platform",
        ),
        sa.CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_platform_accounts_create_hash",
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
    )

    op.create_index(
        "ix_media_platform_accounts_owner_user_id",
        "media_platform_accounts",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_platform_accounts_project_id",
        "media_platform_accounts",
        ["project_id"],
    )
    op.create_index(
        "ix_media_platform_accounts_platform",
        "media_platform_accounts",
        ["platform"],
    )
    op.create_index(
        "ix_media_platform_accounts_create_hash",
        "media_platform_accounts",
        ["create_hash"],
    )
    op.create_index(
        "ix_media_platform_accounts_created_at",
        "media_platform_accounts",
        ["created_at"],
    )
    op.create_index(
        "ix_media_platform_accounts_owner_project",
        "media_platform_accounts",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_platform_accounts_personal_identity",
        "media_platform_accounts",
        ["owner_user_id", "platform", "account_ref"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_platform_accounts_project_identity",
        "media_platform_accounts",
        ["project_id", "platform", "account_ref"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_media_platform_accounts_personal_idempotency",
        "media_platform_accounts",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_platform_accounts_project_idempotency",
        "media_platform_accounts",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "media_platform_account_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column(
            "platform_account_id",
            _uuid(),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            _uuid(),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            _uuid(),
            nullable=True,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "display_name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "publish_capability",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "media_capability",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "analytics_capability",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "credential_status",
            sa.String(length=24),
            nullable=False,
        ),
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name="ck_media_platform_account_revisions_version",
        ),
        sa.CheckConstraint(
            "publish_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_publish",
        ),
        sa.CheckConstraint(
            "media_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_media",
        ),
        sa.CheckConstraint(
            "analytics_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_analytics",
        ),
        sa.CheckConstraint(
            "credential_status IN "
            "('unknown', 'not_configured', 'configured', 'invalid')",
            name="ck_media_platform_account_revisions_credentials",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_platform_account_revisions_hash",
        ),
        sa.ForeignKeyConstraint(
            ["platform_account_id"],
            ["media_platform_accounts.id"],
            ondelete="CASCADE",
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
        sa.UniqueConstraint(
            "platform_account_id",
            "version",
            name="uq_media_platform_account_revisions_version",
        ),
        sa.UniqueConstraint(
            "platform_account_id",
            "idempotency_key",
            name="uq_media_platform_account_revisions_idempotency",
        ),
    )

    op.create_index(
        "ix_media_platform_account_revisions_platform_account_id",
        "media_platform_account_revisions",
        ["platform_account_id"],
    )
    op.create_index(
        "ix_media_platform_account_revisions_owner_user_id",
        "media_platform_account_revisions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_platform_account_revisions_project_id",
        "media_platform_account_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_media_platform_account_revisions_content_hash",
        "media_platform_account_revisions",
        ["content_hash"],
    )
    op.create_index(
        "ix_media_platform_account_revisions_created_at",
        "media_platform_account_revisions",
        ["created_at"],
    )
    op.create_index(
        "ix_media_platform_account_revisions_owner_project",
        "media_platform_account_revisions",
        ["owner_user_id", "project_id"],
    )


def downgrade() -> None:
    op.drop_table("media_platform_account_revisions")
    op.drop_table("media_platform_accounts")
    op.drop_table("media_persona_bulk_draft_slots")
    op.drop_table("media_persona_bulk_drafts")
