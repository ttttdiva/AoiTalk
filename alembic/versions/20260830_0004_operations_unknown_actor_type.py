"""Allow unknown principals in the Operations event timeline.

The Operations HTTP boundary deliberately preserves an ``unknown`` actor type
for authenticated requests that do not carry the web-session authority marker
(for example Bearer requests).  Those requests may create proposals and append
non-authoritative timeline evidence, but human-only decisions remain guarded by
the service layer.  This migration widens only the event actor CHECK and keeps
its downgrade fail-closed once unknown rows have been written.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260830_0004"
down_revision = "20260830_0003"
branch_labels = None
depends_on = None

_TABLE = "operation_events"
_CONSTRAINT = "ck_operation_events_actor_type"
_OLD_EXPRESSION = "actor_type IN ('human','system','agent')"
_NEW_EXPRESSION = "actor_type IN ('human','system','agent','unknown')"


def upgrade() -> None:
    """Replace the event actor CHECK with the converged four-value contract."""

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _NEW_EXPRESSION)


def downgrade() -> None:
    """Restore the old CHECK only when no unknown rows would be stranded."""

    bind = op.get_bind()
    unknown_row = bind.execute(
        sa.text(
            "SELECT 1 FROM operation_events "
            "WHERE actor_type = 'unknown' LIMIT 1"
        )
    ).first()
    if unknown_row is not None:
        raise RuntimeError(
            "cannot downgrade Operations actor_type constraint while "
            "operation_events contains unknown actor rows"
        )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _OLD_EXPRESSION)