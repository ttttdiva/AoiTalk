"""Add project information Docs foundation tables.

Revision ID: 20260701_0004
Revises: 20260701_0003
Create Date: 2026-07-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260701_0004"
down_revision: Union[str, None] = "20260701_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _fk_exists(table_name: str, fk_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(fk["name"] == fk_name for fk in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    if not _column_exists("projects", "knowledge_node_id"):
        op.add_column(
            "projects",
            sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _index_exists("projects", "ix_projects_knowledge_node_id"):
        op.create_index(
            "ix_projects_knowledge_node_id",
            "projects",
            ["knowledge_node_id"],
        )
    if not _fk_exists("projects", "fk_projects_knowledge_node_id"):
        op.create_foreign_key(
            "fk_projects_knowledge_node_id",
            "projects",
            "knowledge_nodes",
            ["knowledge_node_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    if _table_exists("project_qa_entries"):
        return

    op.create_table(
        "project_qa_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("normalized_question_hash", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="unanswered"),
        sa.Column("review_state", sa.String(length=32), nullable=False, server_default="candidate"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("asked_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("source_agent_run_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("source_tool_call_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("answer_source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_agent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_asked_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["conversation_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
    )
    op.create_index("ix_project_qa_entries_project_id", "project_qa_entries", ["project_id"])
    op.create_index(
        "ix_project_qa_entries_knowledge_node_id",
        "project_qa_entries",
        ["knowledge_node_id"],
    )
    op.create_index(
        "ix_project_qa_entries_normalized_question_hash",
        "project_qa_entries",
        ["normalized_question_hash"],
    )
    op.create_index("ix_project_qa_entries_status", "project_qa_entries", ["status"])
    op.create_index(
        "ix_project_qa_entries_review_state",
        "project_qa_entries",
        ["review_state"],
    )
    op.create_index(
        "ix_project_qa_entries_source_session_id",
        "project_qa_entries",
        ["source_session_id"],
    )
    op.create_index("ix_project_qa_entries_deleted_at", "project_qa_entries", ["deleted_at"])
    op.create_index(
        "ix_project_qa_entries_project_review",
        "project_qa_entries",
        ["project_id", "review_state", "status"],
    )


def downgrade() -> None:
    if _table_exists("project_qa_entries"):
        op.drop_index("ix_project_qa_entries_project_review", table_name="project_qa_entries")
        op.drop_index("ix_project_qa_entries_deleted_at", table_name="project_qa_entries")
        op.drop_index("ix_project_qa_entries_source_session_id", table_name="project_qa_entries")
        op.drop_index("ix_project_qa_entries_review_state", table_name="project_qa_entries")
        op.drop_index("ix_project_qa_entries_status", table_name="project_qa_entries")
        op.drop_index(
            "ix_project_qa_entries_normalized_question_hash",
            table_name="project_qa_entries",
        )
        op.drop_index(
            "ix_project_qa_entries_knowledge_node_id",
            table_name="project_qa_entries",
        )
        op.drop_index("ix_project_qa_entries_project_id", table_name="project_qa_entries")
        op.drop_table("project_qa_entries")
