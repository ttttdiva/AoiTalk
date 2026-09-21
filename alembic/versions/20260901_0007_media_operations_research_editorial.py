"""Create MediaOps research evidence ledger and editorial trace boundary."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0007"
down_revision = "20260901_0006"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "media_research_routines",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("create_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_research_routines_create_hash",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_research_routines_owner_user_id",
        "media_research_routines",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_research_routines_project_id",
        "media_research_routines",
        ["project_id"],
    )
    op.create_index(
        "ix_media_research_routines_create_hash",
        "media_research_routines",
        ["create_hash"],
    )
    op.create_index(
        "ix_media_research_routines_created_at",
        "media_research_routines",
        ["created_at"],
    )
    op.create_index(
        "ix_media_research_routines_owner_project",
        "media_research_routines",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_research_routines_personal_idempotency",
        "media_research_routines",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_research_routines_project_idempotency",
        "media_research_routines",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "media_research_routine_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("research_routine_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "questions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
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
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name="ck_media_research_routine_revisions_version",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_research_routine_revisions_hash",
        ),
        sa.ForeignKeyConstraint(
            ["research_routine_id"],
            ["media_research_routines.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_routine_id",
            "version",
            name="uq_media_research_routine_revisions_version",
        ),
        sa.UniqueConstraint(
            "research_routine_id",
            "idempotency_key",
            name="uq_media_research_routine_revisions_idempotency",
        ),
    )
    op.create_index(
        "ix_media_research_routine_revisions_research_routine_id",
        "media_research_routine_revisions",
        ["research_routine_id"],
    )
    op.create_index(
        "ix_media_research_routine_revisions_owner_user_id",
        "media_research_routine_revisions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_research_routine_revisions_project_id",
        "media_research_routine_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_media_research_routine_revisions_content_hash",
        "media_research_routine_revisions",
        ["content_hash"],
    )
    op.create_index(
        "ix_media_research_routine_revisions_created_at",
        "media_research_routine_revisions",
        ["created_at"],
    )
    op.create_index(
        "ix_media_research_routine_revisions_owner_project",
        "media_research_routine_revisions",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_research_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("research_routine_id", _uuid(), nullable=False),
        sa.Column("research_routine_revision_id", _uuid(), nullable=False),
        sa.Column(
            "routine_content_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("focus_note", sa.Text(), nullable=True),
        sa.Column("run_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(routine_content_hash) = 64",
            name="ck_media_research_runs_routine_hash",
        ),
        sa.CheckConstraint(
            "length(run_hash) = 64",
            name="ck_media_research_runs_hash",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["research_routine_id"],
            ["media_research_routines.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_routine_revision_id"],
            ["media_research_routine_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_routine_id",
            "idempotency_key",
            name="uq_media_research_runs_idempotency",
        ),
    )
    op.create_index(
        "ix_media_research_runs_owner_user_id",
        "media_research_runs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_research_runs_project_id",
        "media_research_runs",
        ["project_id"],
    )
    op.create_index(
        "ix_media_research_runs_research_routine_id",
        "media_research_runs",
        ["research_routine_id"],
    )
    op.create_index(
        "ix_media_research_runs_research_routine_revision_id",
        "media_research_runs",
        ["research_routine_revision_id"],
    )
    op.create_index(
        "ix_media_research_runs_run_hash",
        "media_research_runs",
        ["run_hash"],
    )
    op.create_index(
        "ix_media_research_runs_created_at",
        "media_research_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_media_research_runs_owner_project",
        "media_research_runs",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_research_findings",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("research_run_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("finding_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('fact', 'signal', 'hypothesis')",
            name="ck_media_research_findings_kind",
        ),
        sa.CheckConstraint(
            "length(finding_hash) = 64",
            name="ck_media_research_findings_hash",
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["media_research_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_run_id",
            "idempotency_key",
            name="uq_media_research_findings_idempotency",
        ),
        sa.UniqueConstraint(
            "research_run_id",
            "finding_hash",
            name="uq_media_research_findings_hash",
        ),
    )
    op.create_index(
        "ix_media_research_findings_research_run_id",
        "media_research_findings",
        ["research_run_id"],
    )
    op.create_index(
        "ix_media_research_findings_owner_user_id",
        "media_research_findings",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_research_findings_project_id",
        "media_research_findings",
        ["project_id"],
    )
    op.create_index(
        "ix_media_research_findings_kind",
        "media_research_findings",
        ["kind"],
    )
    op.create_index(
        "ix_media_research_findings_finding_hash",
        "media_research_findings",
        ["finding_hash"],
    )
    op.create_index(
        "ix_media_research_findings_created_at",
        "media_research_findings",
        ["created_at"],
    )
    op.create_index(
        "ix_media_research_findings_owner_project",
        "media_research_findings",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_research_finding_evidence",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("finding_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_mime_type", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 20",
            name="ck_media_research_finding_evidence_ordinal",
        ),
        sa.CheckConstraint(
            "evidence_type IN ('url', 'artifact')",
            name="ck_media_research_finding_evidence_type",
        ),
        sa.CheckConstraint(
            "("
            "evidence_type = 'url' "
            "AND source_url IS NOT NULL "
            "AND artifact_sha256 IS NULL "
            "AND artifact_mime_type IS NULL"
            ") OR ("
            "evidence_type = 'artifact' "
            "AND source_url IS NULL "
            "AND artifact_sha256 IS NOT NULL "
            "AND artifact_mime_type IS NOT NULL"
            ")",
            name="ck_media_research_finding_evidence_shape",
        ),
        sa.CheckConstraint(
            "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
            name="ck_media_research_finding_evidence_artifact_hash",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_media_research_finding_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["media_research_findings.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "finding_id",
            "ordinal",
            name="uq_media_research_finding_evidence_ordinal",
        ),
        sa.UniqueConstraint(
            "finding_id",
            "evidence_hash",
            name="uq_media_research_finding_evidence_hash",
        ),
    )
    op.create_index(
        "ix_media_research_finding_evidence_finding_id",
        "media_research_finding_evidence",
        ["finding_id"],
    )
    op.create_index(
        "ix_media_research_finding_evidence_owner_user_id",
        "media_research_finding_evidence",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_research_finding_evidence_project_id",
        "media_research_finding_evidence",
        ["project_id"],
    )
    op.create_index(
        "ix_media_research_finding_evidence_evidence_hash",
        "media_research_finding_evidence",
        ["evidence_hash"],
    )
    op.create_index(
        "ix_media_research_finding_evidence_owner_project",
        "media_research_finding_evidence",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_editorial_programs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("create_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_editorial_programs_create_hash",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"], ["media_personas.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_editorial_programs_owner_user_id",
        "media_editorial_programs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_editorial_programs_project_id",
        "media_editorial_programs",
        ["project_id"],
    )
    op.create_index(
        "ix_media_editorial_programs_persona_id",
        "media_editorial_programs",
        ["persona_id"],
    )
    op.create_index(
        "ix_media_editorial_programs_create_hash",
        "media_editorial_programs",
        ["create_hash"],
    )
    op.create_index(
        "ix_media_editorial_programs_created_at",
        "media_editorial_programs",
        ["created_at"],
    )
    op.create_index(
        "ix_media_editorial_programs_owner_project",
        "media_editorial_programs",
        ["owner_user_id", "project_id"],
    )
    op.create_index(
        "uq_media_editorial_programs_personal_idempotency",
        "media_editorial_programs",
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
        sqlite_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_media_editorial_programs_project_idempotency",
        "media_editorial_programs",
        ["project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )

    op.create_table(
        "media_editorial_program_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("editorial_program_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name="ck_media_editorial_program_revisions_version",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_editorial_program_revisions_hash",
        ),
        sa.ForeignKeyConstraint(
            ["editorial_program_id"],
            ["media_editorial_programs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "editorial_program_id",
            "version",
            name="uq_media_editorial_program_revisions_version",
        ),
        sa.UniqueConstraint(
            "editorial_program_id",
            "idempotency_key",
            name="uq_media_editorial_program_revisions_idempotency",
        ),
    )
    op.create_index(
        "ix_media_editorial_program_revisions_editorial_program_id",
        "media_editorial_program_revisions",
        ["editorial_program_id"],
    )
    op.create_index(
        "ix_media_editorial_program_revisions_owner_user_id",
        "media_editorial_program_revisions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_editorial_program_revisions_project_id",
        "media_editorial_program_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_media_editorial_program_revisions_content_hash",
        "media_editorial_program_revisions",
        ["content_hash"],
    )
    op.create_index(
        "ix_media_editorial_program_revisions_created_at",
        "media_editorial_program_revisions",
        ["created_at"],
    )
    op.create_index(
        "ix_media_editorial_program_revisions_owner_project",
        "media_editorial_program_revisions",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_content_items",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("editorial_program_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_content_items_hash",
        ),
        sa.ForeignKeyConstraint(
            ["editorial_program_id"],
            ["media_editorial_programs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "editorial_program_id",
            "idempotency_key",
            name="uq_media_content_items_idempotency",
        ),
    )
    op.create_index(
        "ix_media_content_items_editorial_program_id",
        "media_content_items",
        ["editorial_program_id"],
    )
    op.create_index(
        "ix_media_content_items_owner_user_id",
        "media_content_items",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_content_items_project_id",
        "media_content_items",
        ["project_id"],
    )
    op.create_index(
        "ix_media_content_items_content_hash",
        "media_content_items",
        ["content_hash"],
    )
    op.create_index(
        "ix_media_content_items_created_at",
        "media_content_items",
        ["created_at"],
    )
    op.create_index(
        "ix_media_content_items_owner_project",
        "media_content_items",
        ["owner_user_id", "project_id"],
    )

    op.create_table(
        "media_content_item_findings",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("content_item_id", _uuid(), nullable=False),
        sa.Column("finding_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 20",
            name="ck_media_content_item_findings_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["media_content_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["media_research_findings.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_item_id",
            "finding_id",
            name="uq_media_content_item_findings_finding",
        ),
        sa.UniqueConstraint(
            "content_item_id",
            "ordinal",
            name="uq_media_content_item_findings_ordinal",
        ),
    )
    op.create_index(
        "ix_media_content_item_findings_content_item_id",
        "media_content_item_findings",
        ["content_item_id"],
    )
    op.create_index(
        "ix_media_content_item_findings_finding_id",
        "media_content_item_findings",
        ["finding_id"],
    )
    op.create_index(
        "ix_media_content_item_findings_owner_user_id",
        "media_content_item_findings",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_media_content_item_findings_project_id",
        "media_content_item_findings",
        ["project_id"],
    )
    op.create_index(
        "ix_media_content_item_findings_owner_project",
        "media_content_item_findings",
        ["owner_user_id", "project_id"],
    )


def downgrade() -> None:
    op.drop_table("media_content_item_findings")
    op.drop_table("media_content_items")
    op.drop_table("media_editorial_program_revisions")
    op.drop_table("media_editorial_programs")
    op.drop_table("media_research_finding_evidence")
    op.drop_table("media_research_findings")
    op.drop_table("media_research_runs")
    op.drop_table("media_research_routine_revisions")
    op.drop_table("media_research_routines")
