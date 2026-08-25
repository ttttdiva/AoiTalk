"""Add bounded reviewable Docs candidates produced by Dreaming Memory."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0003"
down_revision = "20260823_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docs_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "source_type",
            sa.String(length=64),
            nullable=False,
            server_default="dreaming_auto",
        ),
        sa.Column(
            "content_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "sensitivity", sa.String(length=32), nullable=False, server_default="normal"
        ),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("evidence_span", sa.String(length=500), nullable=True),
        sa.Column("source_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="proposed"
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_docs_candidates_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id"], ["scoped_memory_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_docs_candidates_project_status",
        "docs_candidates",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_docs_candidates_source_job",
        "docs_candidates",
        ["source_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_docs_candidates_target_status",
        "docs_candidates",
        ["target_node_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_docs_candidates_target_status", table_name="docs_candidates"
    )
    op.drop_index("ix_docs_candidates_source_job", table_name="docs_candidates")
    op.drop_index(
        "ix_docs_candidates_project_status", table_name="docs_candidates"
    )
    op.drop_table("docs_candidates")
