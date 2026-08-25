"""Add durable settings for external Scoped Memory principals.

External integrations (currently Discord) use a namespaced string owner key
instead of a synthetic ``users`` row.  Context memory and job ownership already
use bounded string columns, so this migration adds only the small settings
store needed for opt-in/opt-out persistence.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_0014"
down_revision = "20260808_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scoped_memory_principals",
        sa.Column("principal_key", sa.String(length=120), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="external",
        ),
        sa.Column(
            "settings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("principal_key"),
    )
    op.create_index(
        "ix_scoped_memory_principals_provider",
        "scoped_memory_principals",
        ["provider"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scoped_memory_principals_provider",
        table_name="scoped_memory_principals",
    )
    op.drop_table("scoped_memory_principals")
