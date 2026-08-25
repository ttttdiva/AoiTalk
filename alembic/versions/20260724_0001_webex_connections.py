"""Webex OAuth 接続と選択スペースを追加する。

Revision ID: 20260724_0001
Revises: 20260719_0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260724_0001"
down_revision = "20260719_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webex_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("webex_person_id", sa.String(length=255), nullable=True),
        sa.Column("webex_org_id", sa.String(length=255), nullable=True),
        sa.Column("webex_email", sa.String(length=255), nullable=True),
        sa.Column("webex_display_name", sa.String(length=255), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_webex_connections_user_id"),
    )
    op.create_index(
        "ix_webex_connections_user_id",
        "webex_connections",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "webex_space_selections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("room_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("room_type", sa.String(length=32), nullable=True),
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
            ["connection_id"],
            ["webex_connections.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "room_id",
            name="uq_webex_space_selections_connection_room",
        ),
    )
    op.create_index(
        "ix_webex_space_selections_connection_id",
        "webex_space_selections",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_webex_space_selections_connection_room",
        "webex_space_selections",
        ["connection_id", "room_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webex_space_selections_connection_room",
        table_name="webex_space_selections",
    )
    op.drop_index(
        "ix_webex_space_selections_connection_id",
        table_name="webex_space_selections",
    )
    op.drop_table("webex_space_selections")
    op.drop_index(
        "ix_webex_connections_user_id",
        table_name="webex_connections",
    )
    op.drop_table("webex_connections")
