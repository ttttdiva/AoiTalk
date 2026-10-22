"""Bind managed product media to an explicit immutable Operations artifact."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261009_0002"
down_revision = "20261009_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "media_asset_revisions",
        sa.Column("artifact_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "media_asset_revisions",
        sa.Column("artifact_binding_hash", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        "ck_media_asset_product_binding",
        "media_asset_revisions",
        "(artifact_version_id IS NULL AND artifact_binding_hash IS NULL) OR (artifact_version_id IS NOT NULL AND artifact_binding_hash IS NOT NULL AND length(artifact_binding_hash) = 64)",
    )
    op.create_foreign_key(
        "fk_media_asset_product_artifact",
        "media_asset_revisions",
        "artifact_versions",
        ["artifact_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade():
    op.drop_constraint(
        "fk_media_asset_product_artifact", "media_asset_revisions", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_media_asset_product_binding", "media_asset_revisions", type_="check"
    )
    op.drop_column("media_asset_revisions", "artifact_binding_hash")
    op.drop_column("media_asset_revisions", "artifact_version_id")
