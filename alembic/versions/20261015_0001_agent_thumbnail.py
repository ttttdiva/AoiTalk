"""Keep the explicitly selected Agent thumbnail separate from execution identity."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261015_0001"
down_revision = "20261012_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("thumbnail_artifact_id", postgresql.UUID(as_uuid=True), nullable=True))
        batch.create_foreign_key("fk_agents_thumbnail_artifact", "artifact_versions", ["thumbnail_artifact_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_thumbnail_artifact", type_="foreignkey")
        batch.drop_column("thumbnail_artifact_id")
