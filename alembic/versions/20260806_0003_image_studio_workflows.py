"""Add the project-scoped Image Studio workflow registry."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0003"
down_revision = "20260806_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_studio_workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=False, server_default="1"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False, server_default="comfyui"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("api_graph", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("ui_workflow", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "key", "version", name="uq_image_studio_workflows_project_key_version"),
    )
    op.create_index("ix_image_studio_workflows_project_id", "image_studio_workflows", ["project_id"])
    op.create_index("ix_image_studio_workflows_created_by", "image_studio_workflows", ["created_by"])
    op.create_index("ix_image_studio_workflows_project_created", "image_studio_workflows", ["project_id", "created_at"])
    op.create_index("ix_image_studio_workflows_project_default", "image_studio_workflows", ["project_id", "is_default"])


def downgrade() -> None:
    op.drop_index("ix_image_studio_workflows_project_default", table_name="image_studio_workflows")
    op.drop_index("ix_image_studio_workflows_project_created", table_name="image_studio_workflows")
    op.drop_index("ix_image_studio_workflows_created_by", table_name="image_studio_workflows")
    op.drop_index("ix_image_studio_workflows_project_id", table_name="image_studio_workflows")
    op.drop_table("image_studio_workflows")
