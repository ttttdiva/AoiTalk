"""Add per-user authentication session versioning.

Incrementing this value invalidates every browser session, JWT, WebSocket
credential, and long-lived API token issued before the credential/authorization
change.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0005"
down_revision = "20260803_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )

    op.add_column(
        "long_lived_api_tokens",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_column("long_lived_api_tokens", "session_version")
    op.drop_column("users", "session_version")
