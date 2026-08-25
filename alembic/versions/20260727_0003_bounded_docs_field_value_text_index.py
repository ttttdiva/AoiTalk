"""Bound the Docs field value text index so long mail values can be stored.

value_text はメール本文や References ヘッダのように数KBになる値も保持する。
生の value_text に btree index を張っていたため、index row size が btree の
上限(2704 bytes)を超えると INSERT 自体が ProgramLimitExceededError で失敗し、
/inbox のメール取り込みが完了できなくなっていた。

既存クエリは ilike や lower(coalesce(...)) 経由でしか value_text を参照せず
生の btree index を利用していないため、先頭500文字に限定した式indexへ置き換える。

Revision ID: 20260727_0003
Revises: 20260727_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0003"
down_revision = "20260727_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_knowledge_field_values_text",
        table_name="knowledge_field_values",
    )
    op.create_index(
        "ix_knowledge_field_values_text",
        "knowledge_field_values",
        [sa.text("left(value_text, 500)")],
    )


def downgrade() -> None:
    # 生の value_text へ戻すと、既に保存済みの長い値で index 作成が失敗する。
    # 値を失わないよう、index対象を上限内の行に限定した部分indexとして戻す。
    op.drop_index(
        "ix_knowledge_field_values_text",
        table_name="knowledge_field_values",
    )
    op.execute(
        "CREATE INDEX ix_knowledge_field_values_text "
        "ON knowledge_field_values (value_text) "
        "WHERE octet_length(value_text) <= 2000"
    )
