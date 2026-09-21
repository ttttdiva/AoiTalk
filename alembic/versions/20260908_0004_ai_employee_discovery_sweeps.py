"""Bound discovery sweeps so continuous arrivals cannot starve late commits.

Revision ID: 20260908_0004
Revises: 20260908_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0004"
down_revision = "20260908_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_automation_discovery_cursors", sa.Column("through_rule_id", sa.UUID(), nullable=True))
    op.add_column("agent_automation_rule_discovery_cursors", sa.Column("sweep_until", sa.DateTime(), nullable=True))
    # Start a bounded sweep; no event/work evidence or execution state changes.
    op.execute(sa.text("UPDATE agent_automation_discovery_cursors SET after_rule_id = NULL"))
    op.execute(sa.text("UPDATE agent_automation_rule_discovery_cursors SET after_occurred_at = NULL, after_event_id = NULL"))


def downgrade():
    # These are discovery optimizations, never action evidence or a work queue.
    op.drop_column("agent_automation_rule_discovery_cursors", "sweep_until")
    op.drop_column("agent_automation_discovery_cursors", "through_rule_id")
