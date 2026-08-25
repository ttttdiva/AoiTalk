"""Add durable Image Studio external browser handoff sessions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0006"
down_revision = "20260806_0005"
branch_labels = None
depends_on = None


def _uuid(
    name: str,
    *,
    nullable: bool = False,
    primary_key: bool = False,
) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        primary_key=primary_key,
    )


def _json(name: str, default: str) -> sa.Column:
    return sa.Column(
        name,
        sa.JSON(),
        nullable=False,
        server_default=sa.text(f"'{default}'::json"),
    )


def upgrade() -> None:
    op.create_table(
        "image_studio_external_sessions",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("generation_id"),
        _uuid("created_by", nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("browser_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        _json("input_snapshot", "{}"),
        _json("payload", "{}"),
        sa.Column("copy_paste_text", sa.Text(), nullable=False, server_default=""),
        _json("result", "{}"),
        _json("attachments", "[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["image_studio_generations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('pending','copied','imported','completed','failed')",
            name="ck_image_studio_external_sessions_status",
        ),
        sa.CheckConstraint(
            "provider IN ('chatgpt','gemini')",
            name="ck_image_studio_external_sessions_provider",
        ),
    )
    op.create_index(
        "ix_image_studio_external_sessions_project_id",
        "image_studio_external_sessions",
        ["project_id"],
    )
    op.create_index(
        "ix_image_studio_external_sessions_generation_id",
        "image_studio_external_sessions",
        ["generation_id"],
    )
    op.create_index(
        "ix_image_studio_external_sessions_created_by",
        "image_studio_external_sessions",
        ["created_by"],
    )
    op.create_index(
        "ix_image_studio_external_sessions_status",
        "image_studio_external_sessions",
        ["status"],
    )
    op.create_index(
        "ix_image_studio_external_sessions_project_created",
        "image_studio_external_sessions",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_external_sessions_project_created",
        table_name="image_studio_external_sessions",
    )
    op.drop_index(
        "ix_image_studio_external_sessions_status",
        table_name="image_studio_external_sessions",
    )
    op.drop_index(
        "ix_image_studio_external_sessions_created_by",
        table_name="image_studio_external_sessions",
    )
    op.drop_index(
        "ix_image_studio_external_sessions_generation_id",
        table_name="image_studio_external_sessions",
    )
    op.drop_index(
        "ix_image_studio_external_sessions_project_id",
        table_name="image_studio_external_sessions",
    )
    op.drop_table("image_studio_external_sessions")
