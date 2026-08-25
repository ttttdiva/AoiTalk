"""TRPG Play セッション image_settings 列追加。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0005"
down_revision = "20260818_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trpg_play_sessions",
        sa.Column(
            "image_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("trpg_play_sessions", "image_settings")
