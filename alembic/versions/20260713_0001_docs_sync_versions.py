"""Docs同期で使う関連行のサーバー版を追加する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260713_0001"
down_revision = "20260712_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("knowledge_node_supertags")}
    if "updated_at" not in columns:
        op.add_column(
            "knowledge_node_supertags",
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.execute(
            sa.text(
                "UPDATE knowledge_node_supertags "
                "SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
                "WHERE updated_at IS NULL"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("knowledge_node_supertags")}
    if "updated_at" in columns:
        op.drop_column("knowledge_node_supertags", "updated_at")
