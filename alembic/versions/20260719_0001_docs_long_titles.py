"""Allow exact long-form Docs nodes such as image-generation prompts.

Revision ID: 20260719_0001
Revises: 20260718_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260719_0001"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("knowledge_nodes", "title", existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=False)
    op.alter_column("knowledge_revisions", "title", existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    # A downgrade must remain executable even after long nodes have been
    # created.  Truncation is intentionally confined to the downgrade path.
    op.execute("UPDATE knowledge_nodes SET title = left(title, 500) WHERE length(title) > 500")
    op.execute("UPDATE knowledge_revisions SET title = left(title, 500) WHERE length(title) > 500")
    op.alter_column("knowledge_revisions", "title", existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=False)
    op.alter_column("knowledge_nodes", "title", existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=False)
