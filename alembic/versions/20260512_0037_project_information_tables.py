"""案件情報カテゴリ・資料・ファクトの正本テーブルを追加

Revision ID: 20260512_0037
Revises: 20260510_0036
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260512_0037"
down_revision = "20260510_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_info_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category_key", sa.String(120), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("source", sa.String(32), nullable=False, server_default="template"),
        sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "category_key", name="uq_project_info_category_key"),
    )
    op.create_index(
        "ix_project_info_categories_project_sort",
        "project_info_categories",
        ["project_id", "sort_order"],
    )
    op.create_index(
        "ix_project_info_categories_status",
        "project_info_categories",
        ["status"],
    )

    op.create_table(
        "project_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_info_categories.id", ondelete="SET NULL"),
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("document_type", sa.String(64), nullable=False, server_default="document"),
        sa.Column("target_kind", sa.String(32), nullable=False, server_default="file"),
        sa.Column("file_path", sa.Text()),
        sa.Column(
            "record_table_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("record_tables.id", ondelete="SET NULL"),
        ),
        sa.Column("external_url", sa.Text()),
        sa.Column("role", sa.String(64), nullable=False, server_default="reference"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_access_level", sa.String(32), nullable=False, server_default="metadata"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("notes", sa.Text()),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.Text()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    op.create_index("ix_project_documents_project_id", "project_documents", ["project_id"])
    op.create_index("ix_project_documents_category_id", "project_documents", ["category_id"])
    op.create_index("ix_project_documents_status", "project_documents", ["status"])
    op.create_index("ix_project_documents_deleted_at", "project_documents", ["deleted_at"])

    op.create_table(
        "project_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_info_categories.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_documents.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("fact_type", sa.String(64), nullable=False, server_default="fact"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.Text()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    op.create_index("ix_project_facts_project_id", "project_facts", ["project_id"])
    op.create_index("ix_project_facts_category_id", "project_facts", ["category_id"])
    op.create_index("ix_project_facts_status", "project_facts", ["status"])
    op.create_index("ix_project_facts_importance", "project_facts", ["importance"])
    op.create_index("ix_project_facts_deleted_at", "project_facts", ["deleted_at"])

    op.create_table(
        "project_info_sync_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(64), nullable=False, server_default="tasks"),
        sa.Column("last_synced_at", sa.DateTime()),
        sa.Column("last_seen_updated_at", sa.DateTime()),
        sa.Column("cursor", sa.JSON()),
        sa.Column("sync_metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id",
            "source_type",
            name="uq_project_info_sync_state_source",
        ),
    )
    op.create_index(
        "ix_project_info_sync_states_project_id",
        "project_info_sync_states",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_info_sync_states_project_id",
        table_name="project_info_sync_states",
    )
    op.drop_table("project_info_sync_states")

    op.drop_index("ix_project_facts_deleted_at", table_name="project_facts")
    op.drop_index("ix_project_facts_importance", table_name="project_facts")
    op.drop_index("ix_project_facts_status", table_name="project_facts")
    op.drop_index("ix_project_facts_category_id", table_name="project_facts")
    op.drop_index("ix_project_facts_project_id", table_name="project_facts")
    op.drop_table("project_facts")

    op.drop_index("ix_project_documents_deleted_at", table_name="project_documents")
    op.drop_index("ix_project_documents_status", table_name="project_documents")
    op.drop_index("ix_project_documents_category_id", table_name="project_documents")
    op.drop_index("ix_project_documents_project_id", table_name="project_documents")
    op.drop_table("project_documents")

    op.drop_index(
        "ix_project_info_categories_status",
        table_name="project_info_categories",
    )
    op.drop_index(
        "ix_project_info_categories_project_sort",
        table_name="project_info_categories",
    )
    op.drop_table("project_info_categories")
