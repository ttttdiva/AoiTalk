"""Atomic retry transitions for durable meeting-processing jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import MeetingProcessingJob


@dataclass(frozen=True)
class RetryCasOutcome:
    won: bool
    job: MeetingProcessingJob | None


def _expected_generation(job: MeetingProcessingJob) -> int:
    return int(getattr(job, "retry_generation", 0) or 0)


def _retry_cas_predicates(
    job: MeetingProcessingJob,
):
    return (
        MeetingProcessingJob.id == job.id,
        MeetingProcessingJob.actor_user_id == job.actor_user_id,
        MeetingProcessingJob.status == "failed",
        MeetingProcessingJob.retryable.is_(True),
        MeetingProcessingJob.retry_generation
        == _expected_generation(job),
        MeetingProcessingJob.lease_owner.is_(None),
        MeetingProcessingJob.lease_token.is_(None),
    )


async def load_actor_job(
    session: AsyncSession,
    *,
    job_id: Any,
    actor_user_id: Any,
) -> MeetingProcessingJob | None:
    result = await session.execute(
        select(MeetingProcessingJob).where(
            MeetingProcessingJob.id == job_id,
            MeetingProcessingJob.actor_user_id == actor_user_id,
        )
    )
    return result.scalar_one_or_none()


async def _reload_after_cas_loss(
    session: AsyncSession,
    job: MeetingProcessingJob,
) -> RetryCasOutcome:
    # Capture identity values before rolling back.  SQLAlchemy expires ORM
    # attributes on rollback (including with expire_on_commit=False), and
    # reading an expired attribute directly from an async session would try to
    # perform implicit IO outside a greenlet.  The captured values are the
    # immutable scope used by the original CAS predicate.
    job_id = job.id
    actor_user_id = job.actor_user_id
    await session.rollback()

    current = await load_actor_job(
        session,
        job_id=job_id,
        actor_user_id=actor_user_id,
    )

    return RetryCasOutcome(
        won=False,
        job=current,
    )


async def requeue_failed_job_cas(
    session: AsyncSession,
    job: MeetingProcessingJob,
) -> RetryCasOutcome:
    """Requeue one exact failed generation.

    The UPDATE itself is the lock/CAS boundary.  This is atomic on both
    PostgreSQL and SQLite.  Encrypted result/error fields are cleared through
    the ORM property while the same transaction still owns the updated row.
    """

    generation = _expected_generation(job)
    now = datetime.utcnow()

    statement = (
        update(MeetingProcessingJob)
        .where(*_retry_cas_predicates(job))
        .values(
            status="queued",
            stage="queued",
            retryable=True,
            retry_generation=generation + 1,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            started_at=None,
            finished_at=None,
            next_attempt_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )

    result = await session.execute(statement)

    if result.rowcount != 1:
        return await _reload_after_cas_loss(session, job)

    # The UPDATE row lock / SQLite writer transaction is still held here.
    # A worker cannot observe queued until commit, so clearing the encrypted
    # fields remains part of the same visible state transition.
    await session.flush()
    await session.refresh(job)

    job.result_json = {}
    job.error_json = {}

    await session.commit()
    await session.refresh(job)

    return RetryCasOutcome(
        won=True,
        job=job,
    )


async def mark_audio_integrity_failed_cas(
    session: AsyncSession,
    job: MeetingProcessingJob,
    *,
    message: str = "Staged audio failed integrity validation",
    details: Mapping[str, Any] | None = None,
) -> RetryCasOutcome:
    """Permanently close a failed generation whose staged audio is invalid."""

    stage = str(job.stage or "queued")
    now = datetime.utcnow()

    statement = (
        update(MeetingProcessingJob)
        .where(*_retry_cas_predicates(job))
        .values(
            retryable=False,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )

    result = await session.execute(statement)

    if result.rowcount != 1:
        return await _reload_after_cas_loss(session, job)

    await session.flush()
    await session.refresh(job)

    job.error_json = {
        "code": "audio.integrity_failed",
        "message": str(message),
        "stage": stage,
        "retryable": False,
        "details": dict(details or {}),
    }

    await session.commit()
    await session.refresh(job)

    return RetryCasOutcome(
        won=True,
        job=job,
    )


__all__ = [
    "RetryCasOutcome",
    "load_actor_job",
    "mark_audio_integrity_failed_cas",
    "requeue_failed_job_cas",
]
