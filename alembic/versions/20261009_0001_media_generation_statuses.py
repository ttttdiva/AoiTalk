"""Align persisted generation-run statuses with the current MediaOps contract."""

from alembic import op
import sqlalchemy as sa


revision = "20261009_0001"
down_revision = "20261007_0025"
branch_labels = None
depends_on = None


_CURRENT_STATUS_CHECK = (
    "status IN ('draft', 'queued', 'claimed', 'running', 'output_pending', "
    "'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', "
    "'unavailable')"
)
_LEGACY_STATUS_CHECK = (
    "status IN ('submitted', 'running', 'succeeded', 'failed', 'uncertain', "
    "'unavailable')"
)
_STATUS_CONSTRAINTS = (
    ("media_generation_runs", "ck_media_generation_runs_status"),
    (
        "media_generation_run_observations",
        "ck_media_generation_run_observations_status",
    ),
)


def upgrade() -> None:
    """Replace the two checks while preserving legacy receipt meaning.

    ``submitted`` was valid in the previously deployed run/observation
    checks, but it is no longer a ``GenerationRunStatus`` value.  The current
    contract represents that accepted-but-not-running state as ``queued``.
    Drop both old checks first so this narrow, meaning-preserving conversion
    can succeed, then add the current checks.
    """

    for table_name, constraint_name in _STATUS_CONSTRAINTS:
        op.drop_constraint(constraint_name, table_name, type_="check")

    for table_name, _constraint_name in _STATUS_CONSTRAINTS:
        op.execute(
            sa.text(
                f"UPDATE {table_name} "
                "SET status = 'queued' "
                "WHERE status = 'submitted'"
            )
        )

    for table_name, constraint_name in _STATUS_CONSTRAINTS:
        op.create_check_constraint(
            constraint_name,
            table_name,
            _CURRENT_STATUS_CHECK,
        )


def downgrade() -> None:
    """Restore the old checks only when no row would become invalid.

    A downgrade never rewrites a run or observation.  In particular, rows
    using ``cancelled`` or ``quarantined`` (or any other newly accepted state)
    make the downgrade fail before either constraint is changed.
    """

    connection = op.get_bind()
    for table_name, _constraint_name in _STATUS_CONSTRAINTS:
        incompatible = connection.execute(
            sa.text(
                f"SELECT status FROM {table_name} "
                f"WHERE status IS NULL OR NOT ({_LEGACY_STATUS_CHECK}) LIMIT 1"
            )
        ).first()
        if incompatible is not None:
            raise RuntimeError(
                "Cannot downgrade media generation statuses: "
                f"{table_name} contains incompatible status "
                f"{incompatible[0]!r}"
            )

    for table_name, constraint_name in reversed(_STATUS_CONSTRAINTS):
        op.drop_constraint(constraint_name, table_name, type_="check")
        op.create_check_constraint(
            constraint_name,
            table_name,
            _LEGACY_STATUS_CHECK,
        )
