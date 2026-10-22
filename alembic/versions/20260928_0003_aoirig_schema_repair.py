"""AoiRigと外部アプリ切り出しの履歴を統合し、欠落したRigテーブルを補修する。

20260928_0002の記録だけがありRigテーブルのない開発DBを観測したため、
既存データを変更せず欠落テーブルを作成する。異なるRig schemaは拒否する。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260928_0003"
down_revision = ("20260928_0001", "20260928_0002")
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    expected = {
        "character_rigs": {"id", "character_id", "owner_id", "active_revision", "source_path", "created_at", "updated_at"},
        "character_rig_builds": {"id", "rig_id", "revision", "status", "format_version", "source_path", "asset_path", "config", "report", "error", "created_at", "updated_at"},
    }
    for table, columns in expected.items():
        if inspector.has_table(table) and {c["name"] for c in inspector.get_columns(table)} != columns:
            raise RuntimeError(f"{table} has an incompatible schema; existing data was not changed")
    if not inspector.has_table("character_rigs"):
        op.create_table("character_rigs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("character_id", UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_id", sa.String(128), nullable=False),
            sa.Column("active_revision", UUID(as_uuid=True)),
            sa.Column("source_path", sa.String(500)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("character_id", "owner_id", name="uq_character_rigs_owner"))
    if not inspector.has_table("character_rig_builds"):
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
    # Repaired tables belong to 0002 and retain user models on this merge downgrade.
    pass
