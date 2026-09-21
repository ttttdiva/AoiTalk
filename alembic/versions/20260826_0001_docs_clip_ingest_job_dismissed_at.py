"""Add dismissed_at to docs_clip_ingest_jobs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260826_0001"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "docs_clip_ingest_jobs",
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("docs_clip_ingest_jobs", "dismissed_at")
