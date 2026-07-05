"""cleanup stale Docs task system field values

Revision ID: 20260705_0009
Revises: 20260704_0008
Create Date: 2026-07-05
"""

from alembic import op


revision = "20260705_0009"
down_revision = "20260704_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM knowledge_field_values fv
        USING knowledge_fields f, knowledge_node_supertags nst, knowledge_supertags st
        WHERE fv.field_id = f.id
          AND fv.node_id = nst.node_id
          AND nst.supertag_id = st.id
          AND st.system_key = 'task'
          AND f.system_key IN (
            'task_status',
            'task_due',
            'task_start',
            'task_priority',
            'task_project'
          )
        """
    )


def downgrade() -> None:
    # Deleted rows were stale duplicates of task-table-backed synthetic values.
    pass
