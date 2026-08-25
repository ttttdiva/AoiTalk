"""Add authenticated Web Push subscriptions.

The notification worker uses these rows to deliver the persisted in-app
notification outside the lifetime/visibility of a browser tab.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260812_0002"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("expiration_time", sa.DateTime(), nullable=True),
        sa.Column(
            "content_encoding",
            sa.String(length=32),
            nullable=False,
            server_default="aes128gcm",
        ),
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
        sa.UniqueConstraint("endpoint", name="uq_web_push_subscriptions_endpoint"),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_id",
        "web_push_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_web_push_subscriptions_user_endpoint",
        "web_push_subscriptions",
        ["user_id", "endpoint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_push_subscriptions_user_endpoint",
        table_name="web_push_subscriptions",
    )
    op.drop_index(
        "ix_web_push_subscriptions_user_id",
        table_name="web_push_subscriptions",
    )
    op.drop_table("web_push_subscriptions")
