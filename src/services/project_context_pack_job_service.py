"""Durable ProjectContextPack rebuild job lifecycle.

The job is intentionally metadata-only: it calls the transaction-aware core
projection builder and never invokes an LLM.  Enqueueing and processing each
open their own database transaction so a caller can safely schedule a rebuild
after a source mutation has committed.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..memory.database import get_db_session
from ..memory.models import Project, ProjectContextPackRebuildJob
from .project_context_pack_service import rebuild_project_context_pack


ACTIVE_JOB_STATUSES = ("pending", "running")
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed"})
DEFAULT_STALE_AFTER_SECONDS = 15 * 60

logger = logging.getLogger(__name__)


def _uuid(value: str | uuid.UUID, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a valid UUID") from exc


def _reason(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean[:128] if clean else None


def _stale_after() -> timedelta:
    raw = os.getenv("AOITALK_PROJECT_CONTEXT_PACK_JOB_STALE_SECONDS")
    try:
        seconds = int(raw) if raw else DEFAULT_STALE_AFTER_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_STALE_AFTER_SECONDS
    return timedelta(seconds=max(1, seconds))


def _is_stale(job: ProjectContextPackRebuildJob, *, now: datetime | None = None) -> bool:
    """Whether a running job can safely be reclaimed by another worker."""

    if job.status != "running":
        return False
    started_at = job.started_at or job.updated_at or job.created_at
    if started_at is None:
        return True
    return (now or datetime.utcnow()) - started_at >= _stale_after()


def _job_payload(job: ProjectContextPackRebuildJob) -> dict[str, Any]:
    return job.to_dict()


async def enqueue_project_context_pack_rebuild(
    project_id: str | uuid.UUID,
    requested_by: str | uuid.UUID,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create at most one pending/running rebuild job for a project.

    The active-row check is performed with a row lock.  PostgreSQL deployments
    additionally serialize this small critical section through the project
    row's lock (``Project`` is selected ``FOR UPDATE``), while lightweight test
    adapters can use the same query without implementing database-specific
    advisory-lock SQL.
    """

    project_uuid = _uuid(project_id, field="project_id")
    actor_uuid = _uuid(requested_by, field="requested_by")
    normalized_reason = _reason(reason)

    async with await get_db_session() as session:
        project = await session.get(Project, project_uuid, with_for_update=True)
        if project is None or getattr(project, "deleted_at", None) is not None:
            raise ValueError("Project not found")

        existing = await session.scalar(
            select(ProjectContextPackRebuildJob)
            .where(
                ProjectContextPackRebuildJob.project_id == project_uuid,
                ProjectContextPackRebuildJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .order_by(ProjectContextPackRebuildJob.created_at.desc())
            .with_for_update()
        )
        if existing is not None:
            await session.commit()
            return _job_payload(existing)

        job = ProjectContextPackRebuildJob(
            id=uuid.uuid4(),
            project_id=project_uuid,
            requested_by=actor_uuid,
            status="pending",
            reason=normalized_reason,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(job)
        await session.flush()
        await session.commit()
        await session.refresh(job)
        return _job_payload(job)


async def process_project_context_pack_rebuild(
    job_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    """Claim and execute one rebuild job.

    ``pending`` jobs are claimed immediately.  A recent ``running`` job is
    left untouched to avoid duplicate workers; a stale running job is safely
    reclaimed.  Any exception is recorded on the job and never escapes the
    worker boundary.
    """

    job_uuid = _uuid(job_id, field="job_id")
    async with await get_db_session() as session:
        job = await session.scalar(
            select(ProjectContextPackRebuildJob)
            .where(ProjectContextPackRebuildJob.id == job_uuid)
            .with_for_update()
        )
        if job is None:
            return None
        if job.status in TERMINAL_JOB_STATUSES:
            return _job_payload(job)
        if job.status == "running":
            if not _is_stale(job):
                return _job_payload(job)
        elif job.status != "pending":
            # Only pending work (or a stale running lease) may be claimed.
            # The database constraint currently prevents other values, but
            # keeping this guard makes the lifecycle fail closed if a legacy
            # adapter returns an unexpected status.
            return _job_payload(job)

        now = datetime.utcnow()
        job.status = "running"
        job.started_at = now
        job.updated_at = now
        job.error_message = None
        await session.flush()

        try:
            pack = await rebuild_project_context_pack(session, job.project_id)
            if isinstance(pack, dict) and pack.get("status") == "failed":
                job.status = "failed"
                job.error_message = "Project context pack rebuild failed"
                job.updated_at = datetime.utcnow()
                await session.commit()
                return _job_payload(job)
            job.status = "completed"
            job.completed_at = datetime.utcnow()
            job.updated_at = job.completed_at
            await session.commit()
        except Exception as exc:
            await session.rollback()
            # Re-open the row after rollback so the error update itself cannot
            # be lost with a failed projection transaction.
            failed = await session.scalar(
                select(ProjectContextPackRebuildJob)
                .where(ProjectContextPackRebuildJob.id == job_uuid)
                .with_for_update()
            )
            if failed is None:
                return None
            failed.status = "failed"
            failed.error_message = str(exc)[:4000]
            failed.updated_at = datetime.utcnow()
            await session.commit()
            return _job_payload(failed)

        return _job_payload(job)


async def run_project_context_pack_rebuild_job(
    job_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    """Run one rebuild from a web background-task boundary.

    ``process_project_context_pack_rebuild`` records projection failures on the
    durable job row.  This outer boundary also catches failures that happen
    before the row can be claimed (for example, a database/session error), so
    Starlette does not surface an unhandled background-task exception after the
    response has already been sent.
    """

    try:
        return await process_project_context_pack_rebuild(job_id)
    except Exception:
        logger.exception("Project context pack rebuild background job failed: %s", job_id)
        return None


async def get_project_context_pack_rebuild_job(
    job_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    """Read one job for diagnostics/tests without exposing source content."""

    job_uuid = _uuid(job_id, field="job_id")
    async with await get_db_session() as session:
        job = await session.get(ProjectContextPackRebuildJob, job_uuid)
        return _job_payload(job) if job is not None else None


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "DEFAULT_STALE_AFTER_SECONDS",
    "enqueue_project_context_pack_rebuild",
    "get_project_context_pack_rebuild_job",
    "process_project_context_pack_rebuild",
    "run_project_context_pack_rebuild_job",
]
