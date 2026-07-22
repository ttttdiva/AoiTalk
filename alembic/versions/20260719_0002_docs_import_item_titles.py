"""Allow import audit rows to mirror long editable Docs titles.

Revision ID: 20260719_0002
Revises: 20260719_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20260719_0002"
down_revision = "20260719_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "knowledge_import_items",
        "title",
        existing_type=sa.String(length=500),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("UPDATE knowledge_import_items SET title = left(title, 500) WHERE length(title) > 500")
    op.alter_column(
        "knowledge_import_items",
        "title",
        existing_type=sa.Text(),
        type_=sa.String(length=500),
        existing_nullable=False,
    )
