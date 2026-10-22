"""Hugging Face / Hydrus 連携機能の廃止に伴い、専用の資格情報テーブルを削除する。

user_hf_credentials / user_hydrus_credentials は HF Explorer / Hydrus 連携
バックエンド（frontend routes・models・services）の削除に伴う専用テーブルで、
他のテーブルから参照されない独立した per-user 資格情報ストアのため、
Story/TRPG の保持テーブルと異なりデータを移行する必要がない。
インデックスはテーブルと共に自動的に削除される。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260928_0001"
down_revision = "20260924_0001"
branch_labels = None
depends_on = None

_TABLES = (
    "user_hf_credentials",
    "user_hydrus_credentials",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


def downgrade() -> None:
    op.create_table(
        "user_hf_credentials",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("encrypted_payload", sa.Text(), nullable=True),
        sa.Column(
            "settings_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_user_hf_credentials_user_id", "user_hf_credentials", ["user_id"]
    )
    op.create_table(
        "user_hydrus_credentials",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("encrypted_payload", sa.Text(), nullable=True),
        sa.Column(
            "settings_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_user_hydrus_credentials_user_id",
        "user_hydrus_credentials",
        ["user_id"],
    )
