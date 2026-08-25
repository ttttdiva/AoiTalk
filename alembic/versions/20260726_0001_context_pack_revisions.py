"""Project context pack generations for safe replacement.

Revision ID: 20260726_0001
Revises: 20260724_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260726_0001"
down_revision = "20260724_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_context_pack_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "context_pack_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["context_pack_id"],
            ["project_context_packs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_context_pack_revisions_context_pack_id",
        "project_context_pack_revisions",
        ["context_pack_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_context_pack_revisions_project_id",
        "project_context_pack_revisions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_context_pack_revisions_pack_revision",
        "project_context_pack_revisions",
        ["context_pack_id", "revision_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_context_pack_revisions_pack_revision",
        table_name="project_context_pack_revisions",
    )
    op.drop_index(
        "ix_project_context_pack_revisions_project_id",
        table_name="project_context_pack_revisions",
    )
    op.drop_index(
        "ix_project_context_pack_revisions_context_pack_id",
        table_name="project_context_pack_revisions",
    )
    op.drop_table("project_context_pack_revisions")
