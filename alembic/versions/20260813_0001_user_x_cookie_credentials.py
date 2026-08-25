"""Add per-user encrypted X Cookie credentials and disabled tombstones."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_0001"
down_revision = "20260812_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_x_cookie_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("encrypted_payload", sa.Text(), nullable=True),
        sa.Column(
            "disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
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
        sa.UniqueConstraint("user_id", name="uq_user_x_cookie_credentials_user_id"),
    )
    op.create_index(
        "ix_user_x_cookie_credentials_user_id",
        "user_x_cookie_credentials",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_x_cookie_credentials_user_id",
        table_name="user_x_cookie_credentials",
    )
    op.drop_table("user_x_cookie_credentials")
