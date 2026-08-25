"""Add Docs system keys and task binding.

Revision ID: 20260704_0006
Revises: 20260704_0005
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260704_0006"
down_revision: Union[str, None] = "20260704_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    """Return whether ``table_name`` exists in the active schema.

    ``Inspector.get_table_names()`` does not include temporary PostgreSQL
    schemas (``pg_temp``), which are used by our disposable migration tests.
    Query ``information_schema`` first so guards operate on the same schema
    that Alembic is upgrading; retain the inspector fallback for offline and
    non-PostgreSQL test binds.
    """
    bind = op.get_bind()
    try:
        return (
            bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                    LIMIT 1
                    """
                ),
                {"table_name": table_name},
            ).first()
            is not None
        )
    except Exception:
        return table_name in sa.inspect(bind).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    try:
        return (
            bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                      AND column_name = :column_name
                    LIMIT 1
                    """
                ),
                {"table_name": table_name, "column_name": column_name},
            ).first()
            is not None
        )
    except Exception:
        inspector = sa.inspect(bind)
        if table_name not in inspector.get_table_names():
            return False
        return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    try:
        return (
            bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = :table_name
                      AND indexname = :index_name
                    LIMIT 1
                    """
                ),
                {"table_name": table_name, "index_name": index_name},
            ).first()
            is not None
        )
    except Exception:
        inspector = sa.inspect(bind)
        if table_name not in inspector.get_table_names():
            return False
        return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    try:
        return (
            bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                      AND constraint_name = :constraint_name
                    LIMIT 1
                    """
                ),
                {"table_name": table_name, "constraint_name": constraint_name},
            ).first()
            is not None
        )
    except Exception:
        inspector = sa.inspect(bind)
        if table_name not in inspector.get_table_names():
            return False
        constraints = [
            *inspector.get_unique_constraints(table_name),
            *inspector.get_foreign_keys(table_name),
        ]
        return any(item.get("name") == constraint_name for item in constraints)


def upgrade() -> None:
    if _table_exists("tasks") and not _column_exists("tasks", "knowledge_node_id"):
        op.add_column(
            "tasks",
            sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if _table_exists("tasks") and not _index_exists("tasks", "ix_tasks_knowledge_node_id"):
        op.create_index("ix_tasks_knowledge_node_id", "tasks", ["knowledge_node_id"], unique=False)
    if _table_exists("tasks") and not _constraint_exists("tasks", "uq_tasks_knowledge_node_id"):
        op.create_unique_constraint("uq_tasks_knowledge_node_id", "tasks", ["knowledge_node_id"])
    if _table_exists("tasks") and not _constraint_exists("tasks", "fk_tasks_knowledge_node_id"):
        op.create_foreign_key(
            "fk_tasks_knowledge_node_id",
            "tasks",
            "knowledge_nodes",
            ["knowledge_node_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if _table_exists("knowledge_supertags") and not _column_exists("knowledge_supertags", "system_key"):
        op.add_column("knowledge_supertags", sa.Column("system_key", sa.Text(), nullable=True))
    if _table_exists("knowledge_supertags") and not _index_exists("knowledge_supertags", "ix_knowledge_supertags_system_key"):
        op.create_index("ix_knowledge_supertags_system_key", "knowledge_supertags", ["system_key"], unique=False)
    if _table_exists("knowledge_supertags") and not _constraint_exists("knowledge_supertags", "uq_knowledge_supertags_workspace_system_key"):
        op.create_unique_constraint(
            "uq_knowledge_supertags_workspace_system_key",
            "knowledge_supertags",
            ["workspace_id", "system_key"],
        )

    if _table_exists("knowledge_fields") and not _column_exists("knowledge_fields", "system_key"):
        op.add_column("knowledge_fields", sa.Column("system_key", sa.Text(), nullable=True))
    if _table_exists("knowledge_fields") and not _index_exists("knowledge_fields", "ix_knowledge_fields_system_key"):
        op.create_index("ix_knowledge_fields_system_key", "knowledge_fields", ["system_key"], unique=False)

    op.execute(
        """
        UPDATE knowledge_supertags
        SET system_key = CASE
            WHEN lower(name) = 'task' THEN 'task'
            WHEN lower(name) = 'meeting' THEN 'meeting'
            WHEN lower(name) = 'person' THEN 'person'
            WHEN name = '案件情報' THEN 'project_info'
            WHEN lower(name) = 'day' THEN 'day'
            ELSE system_key
        END
        WHERE system_key IS NULL
          AND (lower(name) IN ('task', 'meeting', 'person', 'day') OR name = '案件情報')
        """
    )
    op.execute(
        """
        UPDATE knowledge_fields AS f
        SET system_key = CASE
            WHEN t.system_key = 'task' AND f.name = '状態' THEN 'task_status'
            WHEN t.system_key = 'task' AND f.name = '期日' THEN 'task_due'
            WHEN t.system_key = 'task' AND f.name = '開始' THEN 'task_start'
            WHEN t.system_key = 'task' AND f.name = '優先度' THEN 'task_priority'
            WHEN t.system_key = 'task' AND f.name = '案件' THEN 'task_project'
            WHEN t.system_key = 'meeting' AND f.name = '日時' THEN 'meeting_date'
            ELSE f.system_key
        END
        FROM knowledge_supertags AS t
        WHERE f.supertag_id = t.id
          AND f.system_key IS NULL
        """
    )

    if _table_exists("knowledge_edges"):
        op.execute(
            """
            UPDATE knowledge_edges
            SET relation_type = 'inline_ref'
            WHERE relation_type = 'references'
            """
        )


def downgrade() -> None:
    if _table_exists("knowledge_fields") and _index_exists("knowledge_fields", "ix_knowledge_fields_system_key"):
        op.drop_index("ix_knowledge_fields_system_key", table_name="knowledge_fields")
    if _table_exists("knowledge_fields") and _column_exists("knowledge_fields", "system_key"):
        op.drop_column("knowledge_fields", "system_key")

    if _table_exists("knowledge_supertags") and _constraint_exists("knowledge_supertags", "uq_knowledge_supertags_workspace_system_key"):
        op.drop_constraint("uq_knowledge_supertags_workspace_system_key", "knowledge_supertags", type_="unique")
    if _table_exists("knowledge_supertags") and _index_exists("knowledge_supertags", "ix_knowledge_supertags_system_key"):
        op.drop_index("ix_knowledge_supertags_system_key", table_name="knowledge_supertags")
    if _table_exists("knowledge_supertags") and _column_exists("knowledge_supertags", "system_key"):
        op.drop_column("knowledge_supertags", "system_key")

    if _table_exists("tasks") and _constraint_exists("tasks", "fk_tasks_knowledge_node_id"):
        op.drop_constraint("fk_tasks_knowledge_node_id", "tasks", type_="foreignkey")
    if _table_exists("tasks") and _constraint_exists("tasks", "uq_tasks_knowledge_node_id"):
        op.drop_constraint("uq_tasks_knowledge_node_id", "tasks", type_="unique")
    if _table_exists("tasks") and _index_exists("tasks", "ix_tasks_knowledge_node_id"):
        op.drop_index("ix_tasks_knowledge_node_id", table_name="tasks")
    if _table_exists("tasks") and _column_exists("tasks", "knowledge_node_id"):
        op.drop_column("tasks", "knowledge_node_id")
