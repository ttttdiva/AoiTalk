"""Add ON DELETE CASCADE to project foreign keys.

Revision ID: 20260320_0001
Revises: 20260319_0001
Create Date: 2026-03-20 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260320_0001"
down_revision = "20260319_0001"
branch_labels = None
depends_on = None

# (table, constraint_name, column, referenced_table, nullable)
_FK_DEFS = [
    ("tasks", "tasks_project_id_fkey", "project_id", "projects", False),
    ("local_tasks", "local_tasks_project_id_fkey", "project_id", "projects", True),
    ("project_notification_settings", "project_notification_settings_project_id_fkey", "project_id", "projects", False),
    ("notification_deliveries", "notification_deliveries_project_id_fkey", "project_id", "projects", False),
    ("project_rag_collections", "project_rag_collections_project_id_fkey", "project_id", "projects", False),
    ("conversation_sessions", "conversation_sessions_project_id_fkey", "project_id", "projects", True),
]


def upgrade() -> None:
    for table, constraint, column, ref_table, nullable in _FK_DEFS:
        # conversation_sessions: SET NULL on delete (keep chat history)
        on_delete = "SET NULL" if nullable and table == "conversation_sessions" else "CASCADE"
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, ref_table, [column], ["id"], ondelete=on_delete)


def downgrade() -> None:
    for table, constraint, column, ref_table, _nullable in _FK_DEFS:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, ref_table, [column], ["id"])
