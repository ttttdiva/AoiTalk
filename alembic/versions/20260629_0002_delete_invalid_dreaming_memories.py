"""Delete invalid legacy Dreaming memories.

Revision ID: 20260629_0002
Revises: 20260629_0001
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260629_0002"
down_revision = "20260629_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("context_memories"):
        return

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                DELETE FROM context_memories
                WHERE scope_type = 'user'
                  AND (
                    user_id = 'default_user'
                    OR source_type = 'legacy_auto'
                    OR (
                      source_type = 'dreaming_auto'
                      AND (
                        COALESCE(structured_data::jsonb ->> 'sensitivity', '') <> 'normal'
                        OR COALESCE(structured_data::jsonb ->> 'evidence_span', '') = ''
                      )
                    )
                  )
                """
            )
        )
        return

    op.execute(
        sa.text(
            """
            DELETE FROM context_memories
            WHERE scope_type = 'user'
              AND (
                user_id = 'default_user'
                OR source_type = 'legacy_auto'
                OR source_type = 'dreaming_auto'
              )
            """
        )
    )


def downgrade() -> None:
    pass
