"""Persist same-directory full-scan evidence before withdrawing an empty FilesRoot."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261021_0001"
down_revision = "20261018_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No legacy root is presumed healthy and no old empty time is invented.
    op.create_table(
        "workspace_file_root_observations",
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("directory_identity", sa.String(64), nullable=False),
        sa.Column("rules_fingerprint", sa.String(64), nullable=False),
        sa.Column("healthy_at", sa.DateTime(), nullable=False),
        sa.Column("empty_first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("empty_last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("empty_observations", sa.Integer(), nullable=False),
        sa.CheckConstraint("scope IN ('user','project')", name="ck_workspace_file_root_observations_scope"),
    )


def downgrade() -> None:
    op.drop_table("workspace_file_root_observations")
