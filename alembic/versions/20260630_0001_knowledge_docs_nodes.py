"""DB正本Docs用のKnowledgeNode系テーブルを追加

Revision ID: 20260630_0001
Revises: 20260629_0002
Create Date: 2026-06-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260630_0001"
down_revision: Union[str, None] = "20260629_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("settings_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_knowledge_workspaces_owner_user",
        "knowledge_workspaces",
        ["owner_user_id"],
    )

    op.create_table(
        "knowledge_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "root_page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("node_type", sa.String(40), nullable=False, server_default="page"),
        sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime()),
    )
    op.create_index("ix_knowledge_nodes_workspace", "knowledge_nodes", ["workspace_id"])
    op.create_index(
        "ix_knowledge_nodes_workspace_parent_sort",
        "knowledge_nodes",
        ["workspace_id", "parent_id", "sort_order"],
    )
    op.create_index(
        "ix_knowledge_nodes_workspace_project",
        "knowledge_nodes",
        ["workspace_id", "project_id"],
    )
    op.create_index("ix_knowledge_nodes_root_page", "knowledge_nodes", ["root_page_id"])
    op.create_index("ix_knowledge_nodes_archived_at", "knowledge_nodes", ["archived_at"])

    op.create_table(
        "knowledge_supertags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("base_type", sa.String(40), nullable=False, server_default="note"),
        sa.Column("description", sa.Text()),
        sa.Column("icon", sa.String(64)),
        sa.Column("color", sa.String(32)),
        sa.Column("template_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("title_template", sa.Text()),
        sa.Column("ai_instructions", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_knowledge_supertag_name"),
    )
    op.create_index(
        "ix_knowledge_supertags_workspace",
        "knowledge_supertags",
        ["workspace_id"],
    )

    op.create_table(
        "knowledge_node_supertags",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supertag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_supertags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.PrimaryKeyConstraint("node_id", "supertag_id", name="pk_knowledge_node_supertag"),
    )
    op.create_index(
        "ix_knowledge_node_supertags_supertag",
        "knowledge_node_supertags",
        ["supertag_id"],
    )

    op.create_table(
        "knowledge_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supertag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_supertags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("field_type", sa.String(40), nullable=False, server_default="text"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("options_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("default_value_json", sa.JSON()),
        sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("supertag_id", "name", name="uq_knowledge_field_name"),
    )
    op.create_index("ix_knowledge_fields_workspace", "knowledge_fields", ["workspace_id"])
    op.create_index("ix_knowledge_fields_supertag", "knowledge_fields", ["supertag_id"])

    op.create_table(
        "knowledge_field_values",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("value_json", sa.JSON()),
        sa.Column("value_text", sa.Text()),
        sa.Column("value_number", sa.Float()),
        sa.Column("value_datetime", sa.DateTime()),
        sa.Column(
            "target_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.PrimaryKeyConstraint("node_id", "field_id", name="pk_knowledge_field_value"),
    )
    op.create_index("ix_knowledge_field_values_field", "knowledge_field_values", ["field_id"])
    op.create_index(
        "ix_knowledge_field_values_target",
        "knowledge_field_values",
        ["target_node_id"],
    )
    op.create_index(
        "ix_knowledge_field_values_text",
        "knowledge_field_values",
        ["value_text"],
    )
    op.create_index(
        "ix_knowledge_field_values_number",
        "knowledge_field_values",
        ["value_number"],
    )
    op.create_index(
        "ix_knowledge_field_values_datetime",
        "knowledge_field_values",
        ["value_datetime"],
    )

    op.create_table(
        "knowledge_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(80), nullable=False, server_default="related_to"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_edges_source", "knowledge_edges", ["source_node_id"])
    op.create_index("ix_knowledge_edges_target", "knowledge_edges", ["target_node_id"])
    op.create_index("ix_knowledge_edges_relation", "knowledge_edges", ["relation_type"])

    op.create_table(
        "knowledge_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("view_type", sa.String(40), nullable=False, server_default="table"),
        sa.Column("query_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("layout_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_views_workspace", "knowledge_views", ["workspace_id"])

    op.create_table(
        "knowledge_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("change_summary", sa.Text()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_revisions_node", "knowledge_revisions", ["node_id"])

    op.create_table(
        "knowledge_ai_suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        ),
        sa.Column("suggestion_type", sa.String(80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("confidence", sa.Float()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_ai_suggestions_workspace", "knowledge_ai_suggestions", ["workspace_id"])
    op.create_index("ix_knowledge_ai_suggestions_node", "knowledge_ai_suggestions", ["node_id"])
    op.create_index("ix_knowledge_ai_suggestions_status", "knowledge_ai_suggestions", ["status"])

    op.create_table(
        "knowledge_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(120)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("attachment_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_attachments_node", "knowledge_attachments", ["node_id"])

    op.create_table(
        "knowledge_import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("options_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_import_jobs_workspace", "knowledge_import_jobs", ["workspace_id"])
    op.create_index("ix_knowledge_import_jobs_project", "knowledge_import_jobs", ["project_id"])
    op.create_index("ix_knowledge_import_jobs_status", "knowledge_import_jobs", ["status"])

    op.create_table(
        "knowledge_import_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_import_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        ),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("item_type", sa.String(40), nullable=False, server_default="page"),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("preview_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_import_items_job", "knowledge_import_items", ["job_id"])
    op.create_index("ix_knowledge_import_items_node", "knowledge_import_items", ["node_id"])
    op.create_index("ix_knowledge_import_items_status", "knowledge_import_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_import_items_status", table_name="knowledge_import_items")
    op.drop_index("ix_knowledge_import_items_node", table_name="knowledge_import_items")
    op.drop_index("ix_knowledge_import_items_job", table_name="knowledge_import_items")
    op.drop_table("knowledge_import_items")
    op.drop_index("ix_knowledge_import_jobs_status", table_name="knowledge_import_jobs")
    op.drop_index("ix_knowledge_import_jobs_project", table_name="knowledge_import_jobs")
    op.drop_index("ix_knowledge_import_jobs_workspace", table_name="knowledge_import_jobs")
    op.drop_table("knowledge_import_jobs")
    op.drop_index("ix_knowledge_attachments_node", table_name="knowledge_attachments")
    op.drop_table("knowledge_attachments")
    op.drop_index("ix_knowledge_ai_suggestions_status", table_name="knowledge_ai_suggestions")
    op.drop_index("ix_knowledge_ai_suggestions_node", table_name="knowledge_ai_suggestions")
    op.drop_index("ix_knowledge_ai_suggestions_workspace", table_name="knowledge_ai_suggestions")
    op.drop_table("knowledge_ai_suggestions")
    op.drop_index("ix_knowledge_revisions_node", table_name="knowledge_revisions")
    op.drop_table("knowledge_revisions")
    op.drop_index("ix_knowledge_views_workspace", table_name="knowledge_views")
    op.drop_table("knowledge_views")
    op.drop_index("ix_knowledge_edges_relation", table_name="knowledge_edges")
    op.drop_index("ix_knowledge_edges_target", table_name="knowledge_edges")
    op.drop_index("ix_knowledge_edges_source", table_name="knowledge_edges")
    op.drop_table("knowledge_edges")
    op.drop_index("ix_knowledge_field_values_datetime", table_name="knowledge_field_values")
    op.drop_index("ix_knowledge_field_values_number", table_name="knowledge_field_values")
    op.drop_index("ix_knowledge_field_values_text", table_name="knowledge_field_values")
    op.drop_index("ix_knowledge_field_values_target", table_name="knowledge_field_values")
    op.drop_index("ix_knowledge_field_values_field", table_name="knowledge_field_values")
    op.drop_table("knowledge_field_values")
    op.drop_index("ix_knowledge_fields_supertag", table_name="knowledge_fields")
    op.drop_index("ix_knowledge_fields_workspace", table_name="knowledge_fields")
    op.drop_table("knowledge_fields")
    op.drop_index(
        "ix_knowledge_node_supertags_supertag",
        table_name="knowledge_node_supertags",
    )
    op.drop_table("knowledge_node_supertags")
    op.drop_index("ix_knowledge_supertags_workspace", table_name="knowledge_supertags")
    op.drop_table("knowledge_supertags")
    op.drop_index("ix_knowledge_nodes_archived_at", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_root_page", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_workspace_project", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_workspace_parent_sort", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_workspace", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
    op.drop_index("ix_knowledge_workspaces_owner_user", table_name="knowledge_workspaces")
    op.drop_table("knowledge_workspaces")
