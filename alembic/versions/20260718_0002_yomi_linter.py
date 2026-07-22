"""TTS共通読み辞書と誤読候補を追加

Revision ID: 20260718_0002
Revises: 20260718_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260718_0002"
down_revision = "20260718_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yomi_dictionary_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("surface", sa.String(255), nullable=False),
        sa.Column("reading", sa.String(255), nullable=False),
        sa.Column("accent_type", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("target_tts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_yomi_dictionary_surface", "yomi_dictionary_entries", ["surface"])
    op.create_index("ix_yomi_dictionary_enabled", "yomi_dictionary_entries", ["enabled"])
    op.create_table(
        "yomi_unresolved_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("detected_text", sa.String(255), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("tts_engine", sa.String(64), nullable=False),
        sa.Column("dictionary_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="unresolved"),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_yomi_candidate_text", "yomi_unresolved_candidates", ["detected_text"])
    op.create_index("ix_yomi_candidate_engine", "yomi_unresolved_candidates", ["tts_engine"])
    op.create_index("ix_yomi_candidate_status", "yomi_unresolved_candidates", ["status"])
    op.create_table(
        "yomi_dictionary_syncs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dictionary_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tts_engine", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("remote_word_uuid", sa.String(64), nullable=False),
        sa.Column("surface", sa.String(255), nullable=False),
        sa.Column("reading", sa.String(255), nullable=False),
        sa.Column("accent_type", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dictionary_entry_id", "tts_engine", "base_url", name="uq_yomi_sync_target"),
    )
    op.create_index("ix_yomi_sync_entry", "yomi_dictionary_syncs", ["dictionary_entry_id"])
    op.create_index("ix_yomi_sync_engine", "yomi_dictionary_syncs", ["tts_engine"])


def downgrade() -> None:
    op.drop_table("yomi_dictionary_syncs")
    op.drop_table("yomi_unresolved_candidates")
    op.drop_table("yomi_dictionary_entries")
