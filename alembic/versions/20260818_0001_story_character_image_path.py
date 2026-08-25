"""Add image_path to story_characters."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260818_0001"
down_revision = "20260818_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("story_characters", sa.Column("image_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("story_characters", "image_path")
