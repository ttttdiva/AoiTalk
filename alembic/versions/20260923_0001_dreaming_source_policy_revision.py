"""Version Dreaming history source policy and trigger one safe reindex."""

import sqlalchemy as sa
from alembic import op

revision = "20260923_0001"
down_revision = "20260922_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dreaming_memory_states",
        sa.Column("source_policy_revision", sa.String(length=64), nullable=True),
    )


def downgrade():
    op.drop_column("dreaming_memory_states", "source_policy_revision")
