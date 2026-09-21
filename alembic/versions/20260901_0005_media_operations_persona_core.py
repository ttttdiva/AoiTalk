"""Create MediaOps Persona Core and fixed nine-slot intake."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0005"
down_revision = "20260901_0004"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "media_personas",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("create_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_personas_create_hash_length",
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
        "ix_media_personas_owner_user_id",
        "media_personas",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_personas_project_id",
        "media_personas",
        ["project_id"],
    )
    op.create_index(
        "ix_media_personas_create_hash",
        "media_personas",
        ["create_hash"],
    )
    op.create_index(
        "ix_media_personas_created_at",
        "media_personas",
        ["created_at"],
    )
    op.create_index(
        "ix_media_personas_owner_project",
        "media_personas",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_personas_personal_idempotency",
        "media_personas",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_personas_project_idempotency",
        "media_personas",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "media_persona_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("voice", sa.Text(), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column(
            "platform_x",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "platform_pixiv",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "platform_dlsite",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "platform_patreon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "platform_youtube",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "platform_instagram",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "content_pillars",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name="ck_media_persona_revisions_version_positive",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_persona_revisions_content_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"],
            ["media_personas.id"],
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
            "persona_id",
            "version",
            name="uq_media_persona_revisions_version",
        ),
        sa.UniqueConstraint(
            "persona_id",
            "idempotency_key",
            name="uq_media_persona_revisions_idempotency",
        ),
    )
    op.create_index(
        "ix_media_persona_revisions_persona_id",
        "media_persona_revisions",
        ["persona_id"],
    )
    op.create_index(
        "ix_media_persona_revisions_owner_user_id",
        "media_persona_revisions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_persona_revisions_project_id",
        "media_persona_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_media_persona_revisions_content_hash",
        "media_persona_revisions",
        ["content_hash"],
    )
    op.create_index(
        "ix_media_persona_revisions_created_at",
        "media_persona_revisions",
        ["created_at"],
    )
    op.create_index(
        "ix_media_persona_revisions_owner_project",
        "media_persona_revisions",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_persona_resources",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("provenance_type", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_mime_type", sa.String(length=255), nullable=True),
        sa.Column("resource_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "resource_kind IN ('profile', 'reference', 'asset')",
            name="ck_media_persona_resources_kind",
        ),
        sa.CheckConstraint(
            "platform IS NULL OR platform IN "
            "('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_persona_resources_platform",
        ),
        sa.CheckConstraint(
            "provenance_type IN ('url', 'artifact')",
            name="ck_media_persona_resources_provenance_type",
        ),
        sa.CheckConstraint(
            "("
            "provenance_type = 'url' "
            "AND source_url IS NOT NULL "
            "AND artifact_sha256 IS NULL "
            "AND artifact_mime_type IS NULL"
            ") OR ("
            "provenance_type = 'artifact' "
            "AND source_url IS NULL "
            "AND artifact_sha256 IS NOT NULL "
            "AND artifact_mime_type IS NOT NULL"
            ")",
            name="ck_media_persona_resources_provenance_shape",
        ),
        sa.CheckConstraint(
            "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
            name="ck_media_persona_resources_artifact_hash_length",
        ),
        sa.CheckConstraint(
            "length(resource_hash) = 64",
            name="ck_media_persona_resources_resource_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"],
            ["media_personas.id"],
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
            "persona_id",
            "idempotency_key",
            name="uq_media_persona_resources_idempotency",
        ),
    )
    op.create_index(
        "ix_media_persona_resources_persona_id",
        "media_persona_resources",
        ["persona_id"],
    )
    op.create_index(
        "ix_media_persona_resources_owner_user_id",
        "media_persona_resources",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_persona_resources_project_id",
        "media_persona_resources",
        ["project_id"],
    )
    op.create_index(
        "ix_media_persona_resources_resource_hash",
        "media_persona_resources",
        ["resource_hash"],
    )
    op.create_index(
        "ix_media_persona_resources_created_at",
        "media_persona_resources",
        ["created_at"],
    )
    op.create_index(
        "ix_media_persona_resources_owner_project",
        "media_persona_resources",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_persona_intake_slots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "slot >= 1 AND slot <= 9",
            name="ck_media_persona_intake_slots_range",
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
            ["persona_id"],
            ["media_personas.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "persona_id",
            name="uq_media_persona_intake_slots_persona",
        ),
    )
    op.create_index(
        "ix_media_persona_intake_slots_owner_user_id",
        "media_persona_intake_slots",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_persona_intake_slots_project_id",
        "media_persona_intake_slots",
        ["project_id"],
    )
    op.create_index(
        "ix_media_persona_intake_slots_persona_id",
        "media_persona_intake_slots",
        ["persona_id"],
    )
    op.create_index(
        "ix_media_persona_intake_slots_owner_project",
        "media_persona_intake_slots",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_persona_intake_slots_personal_slot",
        "media_persona_intake_slots",
        ["owner_user_id", "slot"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_persona_intake_slots_project_slot",
        "media_persona_intake_slots",
        ["project_id", "slot"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("media_persona_intake_slots")
    op.drop_table("media_persona_resources")
    op.drop_table("media_persona_revisions")
    op.drop_table("media_personas")
