"""Durable background worker for Project Overview refresh jobs."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from ..app_config_store import AppConfigSnapshotUnavailable
from ..memory.database import get_db_session
from ..memory.models import Project, ProjectOverviewRefreshJob
from .project_overview_service import (
    ProjectOverviewNotFound,
    refresh_project_overview,
)

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_OVERVIEW_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_PROJECT_OVERVIEW_STALE_RUNNING_SECONDS = 15 * 60.0

_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")

SessionFactory = Callable[[], Awaitable[Any]]
ConfigLoader = Callable[[], Any | Awaitable[Any]]


def _safe_error_code(value: Any, fallback: str) -> str:
    text = str(value or "").strip().casefold()
    if _SAFE_ERROR_CODE_RE.fullmatch(text):
        return text
    return fallback


class ProjectOverviewWorker:
    """Claim and process durable Project Overview refresh jobs."""

    def __init__(
        self,
        *,
        config: Any,
        db_manager: Any | None = None,
        session_factory: SessionFactory | None = None,
        config_loader: ConfigLoader | None = None,
        poll_interval_seconds: float | None = None,
        stale_running_seconds: float | None = None,
    ) -> None:
        if session_factory is not None:
            self._session_factory = session_factory
        elif db_manager is not None:
            self._session_factory = db_manager.get_session
        else:
            self._session_factory = get_db_session

        configured_poll = poll_interval_seconds
        if configured_poll is None:
            configured_poll = float(
                os.getenv(
                    "AOITALK_PROJECT_OVERVIEW_POLL_INTERVAL_SECONDS",
                    str(DEFAULT_PROJECT_OVERVIEW_POLL_INTERVAL_SECONDS),
                )
            )
        configured_stale = stale_running_seconds
        if configured_stale is None:
            configured_stale = float(
                os.getenv(
                    "AOITALK_PROJECT_OVERVIEW_STALE_RUNNING_SECONDS",
                    str(DEFAULT_PROJECT_OVERVIEW_STALE_RUNNING_SECONDS),
                )
            )

        self.config = config
        # Optional loader lets a long-lived worker take a side-effect-free
        # snapshot of DB-backed configuration for each claimed job.  The
        # server's live Config object remains the default for backwards
        # compatibility and lightweight deployments.
        self._config_loader = config_loader
        self.poll_interval_seconds = max(0.1, float(configured_poll))
        self.stale_running_seconds = max(1.0, float(configured_stale))
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return (
            self._running
            and self._task is not None
            and not self._task.done()
        )

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def _new_session(self) -> Any:
        return await self._session_factory()

    async def _effective_config_snapshot(self) -> Any:
        try:
            source = self._config_loader() if self._config_loader else self.config
            if hasattr(source, "__await__"):
                source = await source
        except AppConfigSnapshotUnavailable:
            raise
        except Exception as exc:
            logger.warning(
                "Project Overview config loader failed: exception_type=%s",
                type(exc).__name__,
            )
            raise AppConfigSnapshotUnavailable() from exc
        # Config objects expose the effective DB-backed dictionary through
        # ``.config``.  Deep-copying prevents a settings update during an
        # in-flight generation from changing the route halfway through a job.
        candidate = getattr(source, "config", source)
        try:
            return copy.deepcopy(candidate)
        except Exception:
            return candidate

    async def recover_stale_running(self) -> int:
        """Recover abandoned running jobs without creating duplicate pending work."""

        cutoff = datetime.utcnow() - timedelta(
            seconds=self.stale_running_seconds
        )
        session = await self._new_session()
        async with session:
            try:
                rows = list(
                    (
                        await session.execute(
                            select(ProjectOverviewRefreshJob)
                            .where(
                                ProjectOverviewRefreshJob.status == "running",
                                ProjectOverviewRefreshJob.started_at.is_not(None),
                                ProjectOverviewRefreshJob.started_at < cutoff,
                            )
                            .order_by(
                                ProjectOverviewRefreshJob.started_at.asc(),
                                ProjectOverviewRefreshJob.id.asc(),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    return 0

                stale_by_project: dict[UUID, list[ProjectOverviewRefreshJob]] = {}
                for job in rows:
                    stale_by_project.setdefault(job.project_id, []).append(job)

                now = datetime.utcnow()
                for project_id in sorted(stale_by_project, key=str):
                    # enqueue_project_overview_refresh serializes through the
                    # Project row before looking for pending work.  Use the
                    # same durable lock here so recovery cannot race a
                    # concurrent enqueue into creating a second pending job.
                    await session.scalar(
                        select(Project.id)
                        .where(Project.id == project_id)
                        .with_for_update()
                    )

                    pending_rows = list(
                        (
                            await session.execute(
                                select(ProjectOverviewRefreshJob)
                                .where(
                                    ProjectOverviewRefreshJob.project_id
                                    == project_id,
                                    ProjectOverviewRefreshJob.status
                                    == "pending",
                                )
                                .order_by(
                                    ProjectOverviewRefreshJob.created_at.asc(),
                                    ProjectOverviewRefreshJob.id.asc(),
                                )
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )

                    retained_pending = (
                        pending_rows[0] if pending_rows else None
                    )
                    for duplicate in pending_rows[1:]:
                        duplicate.status = "failed"
                        duplicate.error_message = "pending_coalesced"
                        duplicate.completed_at = now
                        duplicate.updated_at = now

                    for job in stale_by_project[project_id]:
                        if retained_pending is None:
                            job.status = "pending"
                            job.started_at = None
                            job.completed_at = None
                            job.error_message = None
                            job.updated_at = now
                            retained_pending = job
                            continue

                        # A newer/retained pending request already guarantees
                        # the Project will be refreshed.  Terminalize the
                        # abandoned ownership record instead of producing a
                        # second pending request.
                        job.status = "failed"
                        job.error_message = "stale_running_coalesced"
                        job.completed_at = now
                        job.updated_at = now

                await session.commit()
                return len(rows)
            except Exception:
                await session.rollback()
                raise

    async def _claim_pending(
        self,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest pending job."""

        session = await self._new_session()
        async with session:
            try:
                job = await session.scalar(
                    select(ProjectOverviewRefreshJob)
                    .where(ProjectOverviewRefreshJob.status == "pending")
                    .order_by(
                        ProjectOverviewRefreshJob.created_at.asc(),
                        ProjectOverviewRefreshJob.id.asc(),
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    return None

                project = await session.get(Project, job.project_id)
                if project is None or project.deleted_at is not None:
                    now = datetime.utcnow()
                    job.status = "failed"
                    job.error_message = "project_not_found"
                    job.completed_at = now
                    job.updated_at = now
                    await session.commit()
                    return {
                        "terminal": True,
                        "job": job.to_dict(),
                    }

                now = datetime.utcnow()
                job.status = "running"
                job.started_at = now
                job.completed_at = None
                job.error_message = None
                job.updated_at = now

                await session.commit()
                return {
                    "terminal": False,
                    "job_id": str(job.id),
                    "project_id": str(job.project_id),
                    "requested_by": str(job.requested_by),
                    "reason": str(job.reason or ""),
                }
            except Exception:
                await session.rollback()
                raise

    async def _finish_job(
        self,
        job_id: str | UUID,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"completed", "failed"}:
            raise ValueError("Project Overview job terminal status is invalid")

        session = await self._new_session()
        async with session:
            try:
                job = await session.scalar(
                    select(ProjectOverviewRefreshJob)
                    .where(ProjectOverviewRefreshJob.id == UUID(str(job_id)))
                    .with_for_update()
                )
                if job is None:
                    return None

                if job.status != "running":
                    return job.to_dict()

                now = datetime.utcnow()
                job.status = status
                job.error_message = (
                    _safe_error_code(
                        error_message,
                        "overview_refresh_failed",
                    )
                    if status == "failed"
                    else None
                )
                job.completed_at = now
                job.updated_at = now
                await session.commit()
                return job.to_dict()
            except Exception:
                await session.rollback()
                raise

    async def _process_claim(
        self,
        claim: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(claim["job_id"])
        project_id = str(claim["project_id"])
        requested_by = str(claim["requested_by"])
        reason = str(claim.get("reason") or "project_overview_refresh")

        try:
            effective_config = await self._effective_config_snapshot()
            result = await refresh_project_overview(
                project_id,
                requested_by,
                effective_config,
                reason=reason,
                session_factory=self._session_factory,
            )
        except asyncio.CancelledError:
            raise
        except ProjectOverviewNotFound:
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message="project_not_found",
            )
            return {
                "status": "failed",
                "job": job,
                "overview": None,
            }
        except AppConfigSnapshotUnavailable:
            # Configuration read failures are distinct from provider
            # failures.  The refresh is not invoked, so no provider client can
            # be constructed from stale startup routing; callers receive a
            # stable, secret-free diagnostic code and may retry after the DB
            # configuration store recovers.
            logger.warning(
                "Project Overview refresh config snapshot unavailable: job_id=%s",
                job_id,
            )
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message="config_unavailable",
            )
            return {
                "status": "failed",
                "job": job,
                "overview": None,
                "error_code": "config_unavailable",
            }
        except Exception as exc:
            logger.error(
                "Project Overview refresh job failed unexpectedly: job_id=%s exception_type=%s",
                job_id,
                type(exc).__name__,
            )
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message="overview_refresh_failed",
            )
            return {
                "status": "failed",
                "job": job,
                "overview": None,
            }

        overview_status = str(result.get("status") or "")
        if overview_status == "failed":
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message=_safe_error_code(
                    result.get("error_message"),
                    "overview_refresh_failed",
                ),
            )
            return {
                "status": "failed",
                "job": job,
                "overview": result,
            }

        if overview_status == "pending" and not bool(result.get("requeued")):
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message="overview_refresh_requeue_failed",
            )
            return {
                "status": "failed",
                "job": job,
                "overview": result,
            }

        if overview_status not in {"fresh", "pending"}:
            job = await self._finish_job(
                job_id,
                status="failed",
                error_message="overview_refresh_invalid_state",
            )
            return {
                "status": "failed",
                "job": job,
                "overview": result,
            }

        job = await self._finish_job(
            job_id,
            status="completed",
        )
        return {
            "status": "completed",
            "job": job,
            "overview": result,
        }

    async def run_once(self) -> dict[str, Any]:
        """Recover stale ownership and process at most one pending job."""

        recovered = await self.recover_stale_running()
        claim = await self._claim_pending()
        if claim is None:
            return {
                "status": "idle",
                "recovered": recovered,
            }

        if bool(claim.get("terminal")):
            return {
                "status": "failed",
                "recovered": recovered,
                "job": claim["job"],
                "overview": None,
            }

        result = await self._process_claim(claim)
        result["recovered"] = recovered
        return result

    async def once(self) -> dict[str, Any]:
        """Public one-shot worker API."""

        return await self.run_once()

    async def _run_loop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return

        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Project Overview worker sweep failed")

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        """Start the tracked worker loop; repeated starts are idempotent."""

        if self._task is not None and not self._task.done():
            self._running = True
            return

        self._stop_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(),
            name="aoitalk-project-overview",
        )

    async def stop(self) -> None:
        """Stop and await the tracked worker loop."""

        task = self._task
        event = self._stop_event
        self._task = None
        self._stop_event = None
        self._running = False

        if event is not None:
            event.set()
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "DEFAULT_PROJECT_OVERVIEW_POLL_INTERVAL_SECONDS",
    "DEFAULT_PROJECT_OVERVIEW_STALE_RUNNING_SECONDS",
    "ProjectOverviewWorker",
]
