"""Allow targeted Dreaming source-policy repair run ledgers."""

from alembic import op

revision = "20260923_0002"
down_revision = "20260923_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_dreaming_memory_runs_trigger",
        "dreaming_memory_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dreaming_memory_runs_trigger",
        "dreaming_memory_runs",
        "trigger IN ('idle', 'startup', 'reconcile', 'source_policy_repair')",
    )


def downgrade():
    op.drop_constraint(
        "ck_dreaming_memory_runs_trigger",
        "dreaming_memory_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dreaming_memory_runs_trigger",
        "dreaming_memory_runs",
        "trigger IN ('idle', 'startup', 'reconcile')",
    )
