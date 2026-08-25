"""Image Studio の生成意図と provenance を追加する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0001"
down_revision = "20260804_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_studio_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "conversation_session_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("workflow_key", sa.String(length=160), nullable=True),
        sa.Column("workflow_version", sa.String(length=80), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column(
            "parameters",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "result",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_session_id"],
            ["conversation_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "status IN ('draft','queued','running','succeeded','failed','cancelled')",
            name="ck_image_studio_generations_status",
        ),
    )
    op.create_index(
        "ix_image_studio_generations_project_id",
        "image_studio_generations",
        ["project_id"],
    )
    op.create_index(
        "ix_image_studio_generations_conversation_session_id",
        "image_studio_generations",
        ["conversation_session_id"],
    )
    op.create_index(
        "ix_image_studio_generations_created_by",
        "image_studio_generations",
        ["created_by"],
    )
    op.create_index(
        "ix_image_studio_generations_status",
        "image_studio_generations",
        ["status"],
    )
    op.create_index(
        "ix_image_studio_generations_project_created",
        "image_studio_generations",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_generations_project_created",
        table_name="image_studio_generations",
    )
    op.drop_index(
        "ix_image_studio_generations_status",
        table_name="image_studio_generations",
    )
    op.drop_index(
        "ix_image_studio_generations_created_by",
        table_name="image_studio_generations",
    )
    op.drop_index(
        "ix_image_studio_generations_conversation_session_id",
        table_name="image_studio_generations",
    )
    op.drop_index(
        "ix_image_studio_generations_project_id",
        table_name="image_studio_generations",
    )
    op.drop_table("image_studio_generations")
