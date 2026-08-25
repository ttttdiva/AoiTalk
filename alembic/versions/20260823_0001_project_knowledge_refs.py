"""Add explicit Project-to-KnowledgeNode references."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0001"
down_revision = "20260821_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_knowledge_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_node_id"],
            ["knowledge_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "knowledge_node_id",
            name="uq_project_knowledge_refs_project_node",
        ),
    )
    op.create_index(
        "ix_project_knowledge_refs_project_priority",
        "project_knowledge_refs",
        ["project_id", "priority"],
        unique=False,
    )
    op.create_index(
        "ix_project_knowledge_refs_node",
        "project_knowledge_refs",
        ["knowledge_node_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_knowledge_refs_node",
        table_name="project_knowledge_refs",
    )
    op.drop_index(
        "ix_project_knowledge_refs_project_priority",
        table_name="project_knowledge_refs",
    )
    op.drop_table("project_knowledge_refs")
