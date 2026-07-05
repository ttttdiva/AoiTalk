"""Restore project information and record tables.

Revision ID: 20260701_0003
Revises: 20260630_0002
Create Date: 2026-07-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260701_0003"
down_revision: Union[str, None] = "20260630_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "record_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Float(), nullable=True, server_default="0"),
        sa.Column("schema_version", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("memory_policy", sa.String(length=32), nullable=True, server_default="manual"),
        sa.Column(
            "default_sensitivity",
            sa.String(length=32),
            nullable=True,
            server_default="normal",
        ),
        sa.Column("table_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_record_tables_project_id", "record_tables", ["project_id"])
    op.create_index(
        "ix_record_tables_project_sort",
        "record_tables",
        ["project_id", "sort_order"],
    )
    op.create_index("ix_record_tables_deleted_at", "record_tables", ["deleted_at"])

    op.create_table(
        "record_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("field_type", sa.String(length=32), nullable=False),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("unique_value", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("sort_order", sa.Float(), nullable=True, server_default="0"),
        sa.Column("is_title", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("is_due", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("sensitivity", sa.String(length=32), nullable=True, server_default="normal"),
        sa.Column("field_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["table_id"], ["record_tables.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("table_id", "field_key", name="unique_record_field_key"),
    )
    op.create_index("ix_record_fields_table_id", "record_fields", ["table_id"])
    op.create_index(
        "ix_record_fields_table_sort",
        "record_fields",
        ["table_id", "sort_order"],
    )
    op.create_index("ix_record_fields_deleted_at", "record_fields", ["deleted_at"])

    op.create_table(
        "record_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("values", sa.JSON(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("sensitivity", sa.String(length=32), nullable=True, server_default="normal"),
        sa.Column("row_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["table_id"], ["record_tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_record_rows_table_id", "record_rows", ["table_id"])
    op.create_index("ix_record_rows_project_id", "record_rows", ["project_id"])
    op.create_index("ix_record_rows_due_at", "record_rows", ["due_at"])
    op.create_index("ix_record_rows_deleted_at", "record_rows", ["deleted_at"])
    op.create_index(
        "ix_record_rows_table_updated",
        "record_rows",
        ["table_id", "updated_at"],
    )
    op.create_index(
        "ix_record_rows_project_table",
        "record_rows",
        ["project_id", "table_id"],
    )

    op.create_table(
        "record_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("view_type", sa.String(length=32), nullable=True, server_default="grid"),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Float(), nullable=True, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["table_id"], ["record_tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_record_views_table_id", "record_views", ["table_id"])

    op.create_table(
        "record_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("row_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=True),
        sa.Column("attachment_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["row_id"], ["record_rows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_record_attachments_row_id", "record_attachments", ["row_id"])

    op.create_table(
        "record_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["record_tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["row_id"], ["record_rows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
    )
    op.create_index("ix_record_events_project_id", "record_events", ["project_id"])
    op.create_index("ix_record_events_table_id", "record_events", ["table_id"])
    op.create_index("ix_record_events_row_id", "record_events", ["row_id"])
    op.create_index(
        "ix_record_events_project_created",
        "record_events",
        ["project_id", "created_at"],
    )

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

    op.drop_index("ix_record_events_project_created", table_name="record_events")
    op.drop_index("ix_record_events_row_id", table_name="record_events")
    op.drop_index("ix_record_events_table_id", table_name="record_events")
    op.drop_index("ix_record_events_project_id", table_name="record_events")
    op.drop_table("record_events")

    op.drop_index("ix_record_attachments_row_id", table_name="record_attachments")
    op.drop_table("record_attachments")

    op.drop_index("ix_record_views_table_id", table_name="record_views")
    op.drop_table("record_views")

    op.drop_index("ix_record_rows_project_table", table_name="record_rows")
    op.drop_index("ix_record_rows_table_updated", table_name="record_rows")
    op.drop_index("ix_record_rows_deleted_at", table_name="record_rows")
    op.drop_index("ix_record_rows_due_at", table_name="record_rows")
    op.drop_index("ix_record_rows_project_id", table_name="record_rows")
    op.drop_index("ix_record_rows_table_id", table_name="record_rows")
    op.drop_table("record_rows")

    op.drop_index("ix_record_fields_deleted_at", table_name="record_fields")
    op.drop_index("ix_record_fields_table_sort", table_name="record_fields")
    op.drop_index("ix_record_fields_table_id", table_name="record_fields")
    op.drop_table("record_fields")

    op.drop_index("ix_record_tables_deleted_at", table_name="record_tables")
    op.drop_index("ix_record_tables_project_sort", table_name="record_tables")
    op.drop_index("ix_record_tables_project_id", table_name="record_tables")
    op.drop_table("record_tables")
