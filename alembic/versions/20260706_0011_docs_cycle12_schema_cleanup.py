"""Move Docs cycle 12 schema fixes into migrations.

Revision ID: 20260706_0011
Revises: 20260705_0010
Create Date: 2026-07-06
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "20260706_0011"
down_revision = "20260705_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("tasks")}
    priority = columns.get("priority")
    if priority and priority.get("nullable") is False:
        op.execute(text("alter table tasks alter column priority drop not null"))


def downgrade() -> None:
    # The application intentionally allows Docs-created tasks without a priority.
    # Reintroducing NOT NULL would fail on valid production data, so downgrade is a no-op.
    return None
