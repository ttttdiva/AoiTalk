"""P5 startup migration for fixed Agent revision references.

P5 retires the legacy Team/Profile execution model, while historical
``AgentRevision`` rows remain readable. Unfinished Work/Run rows that point to
those revisions stay pinned to their original revision and retain their
current state and payload. The migration reports how many rows it preserves;
it does not rewrite references, statuses, payloads, or event history.

The public function deliberately receives an existing SQLAlchemy session and
does not commit. The application startup caller can therefore run it in the
same transaction as its other startup migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Text, cast, or_, select

from ..memory.models import AgentRevision, AgentRun, AgentWorkItem


P5_AGENT_RUNTIME_MIGRATION_ID = "agent_runtime_v2_removal"

# These are the non-terminal states defined by the durable models. Keep the
# sets local to this migration so importing runtime services is not required
# during application bootstrap.
WORK_ITEM_MIGRATION_STATES = frozenset(
    {
        "pending",
        "claimed",
        "running",
        "awaiting_approval",
        "blocked",
        "retry_wait",
        "uncertain",
    }
)
RUN_MIGRATION_STATES = frozenset(
    {"queued", "running", "awaiting_approval", "uncertain"}
)


@dataclass(slots=True)
class AgentRuntimeMigrationReport:
    """Counts of unfinished rows kept on their historical Agent revision."""

    migration_id: str = P5_AGENT_RUNTIME_MIGRATION_ID
    preserved_work_items: int = 0
    preserved_runs: int = 0

    @property
    def preserved_total(self) -> int:
        return self.preserved_work_items + self.preserved_runs

    def to_dict(self) -> dict[str, str | int]:
        return {
            "migration_id": self.migration_id,
            "preserved_work_items": self.preserved_work_items,
            "preserved_runs": self.preserved_runs,
            "preserved_total": self.preserved_total,
        }


def migrate_legacy_agent_runtime_references(
    session: Any, *, now: datetime | None = None
) -> AgentRuntimeMigrationReport:
    """Count unfinished rows pinned to legacy revisions without changing them.

    The caller must commit or roll back the supplied session. No production
    database is opened by this helper, which also makes it suitable for an
    isolated startup migration test. ``now`` is retained for call
    compatibility; preservation performs no timestamp updates.
    """

    del now
    report = AgentRuntimeMigrationReport()
    legacy_revisions = session.execute(
        select(AgentRevision).where(
            or_(
                AgentRevision.model_overrides.is_(None),
                cast(AgentRevision.model_overrides, Text) == "null",
                AgentRevision.tool_ids.is_(None),
                cast(AgentRevision.tool_ids, Text) == "null",
                AgentRevision.delegate_policy.is_(None),
                cast(AgentRevision.delegate_policy, Text) == "null",
            )
        )
    ).scalars().all()

    for legacy_revision in legacy_revisions:
        work_items = session.execute(
            select(AgentWorkItem)
            .where(
                AgentWorkItem.agent_revision_id == legacy_revision.id,
                AgentWorkItem.state.in_(tuple(WORK_ITEM_MIGRATION_STATES)),
            )
            .with_for_update()
        ).scalars().all()
        runs = session.execute(
            select(AgentRun)
            .where(
                AgentRun.agent_revision_id == legacy_revision.id,
                AgentRun.status.in_(tuple(RUN_MIGRATION_STATES)),
            )
            .with_for_update()
        ).scalars().all()

        # The row locks protect the observed unfinished records for the
        # caller-owned transaction. In particular, do not attach events or
        # rewrite any fields: a future compatible runtime can resume each row
        # against the exact revision and payload that created it.
        report.preserved_work_items += len(work_items)
        report.preserved_runs += len(runs)

    return report


# Explicit alias for startup callers that prefer the migration wording.
run_agent_runtime_p5_migration = migrate_legacy_agent_runtime_references


__all__ = [
    "AgentRuntimeMigrationReport",
    "P5_AGENT_RUNTIME_MIGRATION_ID",
    "RUN_MIGRATION_STATES",
    "WORK_ITEM_MIGRATION_STATES",
    "migrate_legacy_agent_runtime_references",
    "run_agent_runtime_p5_migration",
]
