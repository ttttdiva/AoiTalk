"""モデル別の会話再生設定をimmutable候補と分離して保存する。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261001_0001"
down_revision = "20260928_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "character_rig_playback_settings",
        sa.Column("build_id", UUID(as_uuid=True), sa.ForeignKey("character_rig_builds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("character_rig_playback_settings")
