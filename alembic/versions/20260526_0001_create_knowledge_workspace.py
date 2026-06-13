"""create knowledge workspace tables

Revision ID: 20260526_0001
Revises: 20260518_0041
Create Date: 2026-05-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260526_0001"
down_revision: Union[str, None] = "20260518_0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_rag_collections CASCADE")
    op.execute("DROP TABLE IF EXISTS project_rag_collections CASCADE")
    op.execute("DROP TABLE IF EXISTS rag_collections CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_note_classifications CASCADE")

    op.create_table(
        "knowledge_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("access_policy", sa.JSON(), nullable=True),
        sa.Column("include_patterns", sa.JSON(), nullable=True),
        sa.Column("exclude_patterns", sa.JSON(), nullable=True),
        sa.Column("sync_mode", sa.String(length=20), nullable=False),
        sa.Column("write_policy", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_sources_source_type", "knowledge_sources", ["source_type"])
    op.create_index("ix_knowledge_sources_status", "knowledge_sources", ["status"])

    op.create_table(
        "knowledge_source_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("permission", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_knowledge_source_permissions_source_user",
        "knowledge_source_permissions",
        ["source_id", "user_id"],
    )
    op.create_index(
        "ix_knowledge_source_permissions_source_project",
        "knowledge_source_permissions",
        ["source_id", "project_id"],
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("resolved_absolute_path", sa.Text(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("extension", sa.String(length=32), nullable=True),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("modified_at", sa.DateTime(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("frontmatter_json", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("project_refs", sa.JSON(), nullable=True),
        sa.Column("task_refs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("source_id", "path", name="uq_knowledge_document_source_path"),
    )
    op.create_index("ix_knowledge_documents_extension", "knowledge_documents", ["extension"])
    op.create_index("ix_knowledge_documents_content_hash", "knowledge_documents", ["content_hash"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    op.create_index(
        "ix_knowledge_documents_source_status",
        "knowledge_documents",
        ["source_id", "status"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("heading_path", sa.JSON(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("vector_id", sa.String(length=100), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
    )
    op.create_index("ix_knowledge_chunks_content_hash", "knowledge_chunks", ["content_hash"])
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])

    op.create_table(
        "knowledge_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_path_or_url", sa.Text(), nullable=False),
        sa.Column("link_type", sa.String(length=20), nullable=False),
        sa.Column(
            "resolved_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_links_source_document", "knowledge_links", ["source_document_id"])
    op.create_index("ix_knowledge_links_resolved_document", "knowledge_links", ["resolved_document_id"])

    op.create_table(
        "knowledge_annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("annotation_type", sa.String(length=40), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_annotations_annotation_type", "knowledge_annotations", ["annotation_type"])
    op.create_index("ix_knowledge_annotations_status", "knowledge_annotations", ["status"])
    op.create_index(
        "ix_knowledge_annotations_document_status",
        "knowledge_annotations",
        ["document_id", "status"],
    )

    op.create_table(
        "knowledge_edit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pre_hash", sa.String(length=64), nullable=True),
        sa.Column("post_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_edit_events_status", "knowledge_edit_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_edit_events_status", table_name="knowledge_edit_events")
    op.drop_table("knowledge_edit_events")
    op.drop_index("ix_knowledge_annotations_document_status", table_name="knowledge_annotations")
    op.drop_index("ix_knowledge_annotations_status", table_name="knowledge_annotations")
    op.drop_index("ix_knowledge_annotations_annotation_type", table_name="knowledge_annotations")
    op.drop_table("knowledge_annotations")
    op.drop_index("ix_knowledge_links_resolved_document", table_name="knowledge_links")
    op.drop_index("ix_knowledge_links_source_document", table_name="knowledge_links")
    op.drop_table("knowledge_links")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_content_hash", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_source_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_content_hash", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_extension", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index(
        "ix_knowledge_source_permissions_source_project",
        table_name="knowledge_source_permissions",
    )
    op.drop_index(
        "ix_knowledge_source_permissions_source_user",
        table_name="knowledge_source_permissions",
    )
    op.drop_table("knowledge_source_permissions")
    op.drop_index("ix_knowledge_sources_status", table_name="knowledge_sources")
    op.drop_index("ix_knowledge_sources_source_type", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
