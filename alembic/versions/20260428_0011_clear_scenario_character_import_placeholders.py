"""Clear placeholder scenario character import backstories.

Revision ID: 20260428_0011
Revises: 20260424_0010
Create Date: 2026-04-28 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260428_0011"
down_revision = "20260424_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE scenario_characters
        SET backstory = ''
        WHERE backstory LIKE '旧シナリオ資料から移行:%'
        """
    )


def downgrade() -> None:
    # The original placeholder source path is intentionally not restored.
    pass
