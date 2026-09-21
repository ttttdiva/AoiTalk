"""Add UTC semantic dates to Knowledge documents.

Revision ID: 20260908_0001
Revises: 20260904_0001
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260908_0001"
down_revision: Union[str, None] = "20260904_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("document_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("document_date_source", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_knowledge_documents_document_date",
        "knowledge_documents",
        ["document_date"],
    )
    op.create_index(
        "ix_knowledge_documents_document_date_source",
        "knowledge_documents",
        ["document_date_source"],
    )

    # Existing rows have no safe way to recover frontmatter or filename
    # semantics inside a schema migration.  Preserve their old ordering by
    # recording modified_at as an explicit UTC fallback; the next source sync
    # recomputes frontmatter/GROWI/filename metadata deterministically.
    op.execute(
        sa.text(
            """
            UPDATE knowledge_documents
            SET document_date = modified_at AT TIME ZONE 'UTC',
                document_date_source = 'modified_at'
            WHERE document_date IS NULL AND modified_at IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_documents_document_date_source",
        table_name="knowledge_documents",
    )
    op.drop_index(
        "ix_knowledge_documents_document_date",
        table_name="knowledge_documents",
    )
    op.drop_column("knowledge_documents", "document_date_source")
    op.drop_column("knowledge_documents", "document_date")
