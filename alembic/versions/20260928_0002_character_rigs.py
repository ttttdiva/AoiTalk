"""ユーザー別AoiRigと失敗時に切替されない生成リビジョン。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260928_0002"
down_revision = "20260924_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("character_rigs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("character_id", UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("active_revision", UUID(as_uuid=True)),
        sa.Column("source_path", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("character_id", "owner_id", name="uq_character_rigs_owner"))
    op.create_table("character_rig_builds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rig_id", UUID(as_uuid=True), sa.ForeignKey("character_rigs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(500), nullable=False),
        sa.Column("asset_path", sa.String(500)),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("rig_id", "revision", name="uq_character_rig_revision"))
    op.create_index("ix_character_rig_build_status", "character_rig_builds", ["status"])


def downgrade():
    op.drop_table("character_rig_builds")
    op.drop_table("character_rigs")
