"""Default recurring task timezone to Asia/Tokyo.

Revision ID: 20260507_0028
Revises: 20260507_0027
Create Date: 2026-05-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.task_time import DEFAULT_TASK_TIMEZONE


revision: str = "20260507_0028"
down_revision: Union[str, None] = "20260507_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "task_recurrence_rules",
        "timezone",
        server_default=DEFAULT_TASK_TIMEZONE,
        existing_type=sa.String(length=64),
    )


def downgrade() -> None:
    op.alter_column(
        "task_recurrence_rules",
        "timezone",
        server_default="UTC",
        existing_type=sa.String(length=64),
    )
