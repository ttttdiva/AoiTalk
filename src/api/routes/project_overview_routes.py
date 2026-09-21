"""Authenticated Project Overview HTTP routes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from ...app_config_store import AppConfigSnapshotUnavailable
from ...memory.models import (
    ContextMemory,
    ProjectOverview,
    ProjectOverviewRefreshJob,
)
from ...memory.project_repository import ProjectRepository
from ...services.project_overview_schema import (
    build_project_memory_digest,
    empty_project_overview_layout,
)
from ...services.project_overview_service import (
    ProjectOverviewNotFound,
    ProjectOverviewServiceError,
    build_project_overview_diagnostics,
    enqueue_project_overview_refresh,
)


logger = logging.getLogger(__name__)


class ProjectOverviewMemoryRef(BaseModel):
    id: str
    title: str | None = None
    memory_type: str
    content: str
    importance: int
    confidence: float
    is_pinned: bool
    updated_at: str | None = None


class ProjectOverviewResponse(BaseModel):
    project_id: str
    status: str
    layout: dict[str, Any]
    source_digest: str | None = None
    generated_at: str | None = None
    generation_version: int = 1
    error_message: str | None = None
    memory_refs: dict[str, ProjectOverviewMemoryRef] = Field(
        default_factory=dict
    )


class ProjectOverviewRefreshResponse(BaseModel):
    job_id: str
    project_id: str
    requested_by: str
    status: str
    reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ProjectOverviewDiagnosticsResponse(BaseModel):
    """Secret-free operator diagnostic projection for one Overview."""

    project_id: str
    status: str
    error_code: str | None = None
    stage: str
    code: str
    source: str
    retryable: bool
    action: str | None = None
    has_last_known_good: bool
    route_healthy: bool
    historical_failure: bool = False
    route: dict[str, Any] = Field(default_factory=dict)
    latest_job: dict[str, Any] | None = None


def _layout_memory_ids(layout: Any) -> set[UUID]:
    if not isinstance(layout, dict):
        return set()

    raw_ids: list[Any] = []
    sections = layout.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            memory_ids = section.get("memory_ids")
            if isinstance(memory_ids, list):
                raw_ids.extend(memory_ids)

    graph = layout.get("graph")
    if isinstance(graph, dict):
        for collection_name in ("nodes", "edges"):
            collection = graph.get(collection_name)
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                memory_ids = item.get("memory_ids")
                if isinstance(memory_ids, list):
                    raw_ids.extend(memory_ids)

    result: set[UUID] = set()
    for raw in raw_ids:
        try:
            result.add(UUID(str(raw)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _memory_ref_payload(memory: ContextMemory) -> dict[str, Any]:
    return {
        "id": str(memory.id),
        "title": memory.title,
        "memory_type": str(memory.memory_type or "fact"),
        "content": str(memory.content or "")[:2000],
        "importance": int(memory.importance or 0),
        "confidence": float(memory.confidence or 0.0),
        "is_pinned": bool(memory.is_pinned),
        "updated_at": (
            memory.updated_at.isoformat()
            if memory.updated_at is not None
            else None
        ),
    }


async def _active_memory_refs(
    session: Any,
    *,
    project_id: UUID,
    layout: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    ids = _layout_memory_ids(layout)
    if not ids:
        return {}

    rows = list(
        (
            await session.execute(
                select(ContextMemory).where(
                    ContextMemory.id.in_(ids),
                    ContextMemory.project_id == project_id,
                    ContextMemory.scope_type == "project",
                    ContextMemory.status == "active",
                    or_(
                        ContextMemory.expires_at.is_(None),
                        ContextMemory.expires_at > datetime.utcnow(),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        str(memory.id): _memory_ref_payload(memory)
        for memory in rows
    }


async def _has_active_project_memory(
    session: Any,
    *,
    project_id: UUID,
) -> bool:
    memory_id = await session.scalar(
        select(ContextMemory.id)
        .where(
            ContextMemory.project_id == project_id,
            ContextMemory.scope_type == "project",
            ContextMemory.status == "active",
            or_(
                ContextMemory.expires_at.is_(None),
                ContextMemory.expires_at > datetime.utcnow(),
            ),
        )
        .limit(1)
    )
    return memory_id is not None


def _overview_payload(
    project_id: UUID,
    overview: ProjectOverview | None,
    memory_refs: dict[str, dict[str, Any]],
    *,
    missing_status: str = "pending",
    missing_source_digest: str | None = None,
) -> dict[str, Any]:
    if overview is None:
        return {
            "project_id": str(project_id),
            "status": missing_status,
            "layout": empty_project_overview_layout(),
            "source_digest": missing_source_digest,
            "generated_at": None,
            "generation_version": 1,
            "error_message": None,
            "memory_refs": {},
        }

    return {
        "project_id": str(project_id),
        "status": str(overview.status or "pending"),
        "layout": overview.layout_json or empty_project_overview_layout(),
        "source_digest": overview.source_digest,
        "generated_at": (
            overview.generated_at.isoformat()
            if overview.generated_at is not None
            else None
        ),
        "generation_version": int(overview.generation_version or 1),
        "error_message": overview.error_message,
        "memory_refs": memory_refs,
    }


def create_project_overview_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    get_config=None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/projects",
        tags=["project-overview"],
    )

    async def _actor(request: Request) -> dict[str, Any]:
        actor = await get_user_from_request(request)
        if not actor or not actor.get("id"):
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
            )
        return actor

    async def _authorized_project_session(
        project_id: UUID,
        request: Request,
    ) -> tuple[Any, dict[str, Any]]:
        actor = await _actor(request)
        try:
            actor_id = UUID(str(actor["id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=403,
                detail="Project access denied",
            ) from exc

        manager = get_db_manager()
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable",
            )
        session = await manager.get_session()
        project = await ProjectRepository.get_by_id(
            session,
            project_id,
        )
        if project is None:
            await session.close()
            raise HTTPException(
                status_code=404,
                detail="Project not found",
            )
        allowed = await ProjectRepository.has_permission(
            session,
            project_id,
            actor_id,
            "read",
        )
        if not allowed:
            await session.close()
            raise HTTPException(
                status_code=403,
                detail="Project access denied",
            )
        return session, actor

    async def _session_factory() -> Any:
        manager = get_db_manager()
        if manager is None:
            raise RuntimeError("database unavailable")
        return await manager.get_session()

    async def _effective_config() -> Any:
        """Read the current effective config without mutating server state."""

        if callable(get_config):
            try:
                value = get_config()
                if hasattr(value, "__await__"):
                    value = await value
                return value
            except Exception as exc:
                logger.warning(
                    "Project Overview diagnostics config callback unavailable: exception_type=%s",
                    type(exc).__name__,
                )
                if isinstance(exc, AppConfigSnapshotUnavailable):
                    raise
                raise AppConfigSnapshotUnavailable() from exc

        # The server normally supplies its live Config object through
        # ``get_config``.  Keep a defensive fallback for lightweight route
        # registration/tests and rolling deployments where that callback is
        # not available yet; the store's migration/seed behavior remains the
        # single source of truth.
        try:
            from ...app_config_store import load_app_config_snapshot_sync

            return await asyncio.to_thread(load_app_config_snapshot_sync)
        except Exception as exc:
            logger.warning(
                "Project Overview diagnostics config snapshot unavailable: exception_type=%s",
                type(exc).__name__,
            )
            if isinstance(exc, AppConfigSnapshotUnavailable):
                raise
            raise AppConfigSnapshotUnavailable() from exc

    @router.get(
        "/{project_id}/overview",
        response_model=ProjectOverviewResponse,
    )
    async def get_project_overview(
        project_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        session, actor_info = await _authorized_project_session(
            project_id,
            request,
        )
        should_enqueue = False
        try:
            overview = await session.scalar(
                select(ProjectOverview).where(
                    ProjectOverview.project_id == project_id
                )
            )

            if overview is None:
                has_active_memory = await _has_active_project_memory(
                    session,
                    project_id=project_id,
                )
                should_enqueue = has_active_memory
                payload = _overview_payload(
                    project_id,
                    None,
                    {},
                    missing_status=(
                        "pending"
                        if has_active_memory
                        else "fresh"
                    ),
                    missing_source_digest=(
                        None
                        if has_active_memory
                        else build_project_memory_digest(())
                    ),
                )
            else:
                layout = (
                    overview.layout_json
                    if isinstance(overview.layout_json, dict)
                    else empty_project_overview_layout()
                )
                refs = await _active_memory_refs(
                    session,
                    project_id=project_id,
                    layout=layout,
                )
                payload = _overview_payload(
                    project_id,
                    overview,
                    refs,
                )
        finally:
            await session.close()

        if should_enqueue:
            try:
                await enqueue_project_overview_refresh(
                    project_id,
                    str(actor_info["id"]),
                    "overview_row_missing",
                    session_factory=_session_factory,
                )
            except Exception:
                logger.warning(
                    "Project Overview row-missing refresh enqueue failed: %s",
                    project_id,
                    exc_info=True,
                )

        return payload

    @router.post(
        "/{project_id}/overview/refresh",
        response_model=ProjectOverviewRefreshResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def refresh_project_overview_route(
        project_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        session, actor = await _authorized_project_session(
            project_id,
            request,
        )
        await session.close()

        try:
            job = await enqueue_project_overview_refresh(
                project_id,
                str(actor["id"]),
                "manual_refresh",
                session_factory=_session_factory,
                raise_on_error=True,
            )
        except ProjectOverviewNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="Project not found",
            ) from exc
        except ProjectOverviewServiceError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Project Overview refresh could not be queued",
            ) from exc

        if not isinstance(job, dict) or not job.get("id"):
            raise HTTPException(
                status_code=503,
                detail="Project Overview refresh could not be queued",
            )

        return {
            "job_id": str(job["id"]),
            "project_id": str(job.get("project_id") or project_id),
            "requested_by": str(job.get("requested_by") or actor["id"]),
            "status": str(job.get("status") or "pending"),
            "reason": job.get("reason"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }

    @router.get(
        "/{project_id}/overview/diagnostics",
        response_model=ProjectOverviewDiagnosticsResponse,
    )
    async def get_project_overview_diagnostics(
        project_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        """Return operator-safe route/failure diagnostics.

        Read ACL alone is not sufficient: provider/model routing metadata is
        restricted to Project ``manage_settings`` operators (plus admins).
        Values are reduced to identifiers and configuration-presence flags;
        credentials, endpoint URLs, headers, and exception messages never
        cross this boundary.
        """

        session, actor = await _authorized_project_session(project_id, request)
        try:
            try:
                actor_id = UUID(str(actor["id"]))
            except (TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(
                    status_code=403,
                    detail="Project diagnostics access denied",
                ) from exc

            is_admin = str(actor.get("role") or "").strip().casefold() == "admin"
            if not is_admin:
                allowed = await ProjectRepository.has_permission(
                    session,
                    project_id,
                    actor_id,
                    "manage_settings",
                )
                if not allowed:
                    raise HTTPException(
                        status_code=403,
                        detail="Project diagnostics access denied",
                    )

            overview = await session.scalar(
                select(ProjectOverview).where(
                    ProjectOverview.project_id == project_id
                )
            )

            latest_job = None
            execute = getattr(session, "execute", None)
            if callable(execute):
                try:
                    latest_job = await session.scalar(
                        select(ProjectOverviewRefreshJob)
                        .where(
                            ProjectOverviewRefreshJob.project_id == project_id
                        )
                        .order_by(
                            ProjectOverviewRefreshJob.created_at.desc(),
                            ProjectOverviewRefreshJob.id.desc(),
                        )
                        .limit(1)
                    )
                except Exception as exc:
                    # Diagnostics should remain available when a rolling
                    # deployment has not yet installed the refresh-job table.
                    logger.warning(
                        "Project Overview diagnostics latest job unavailable: project_id=%s exception_type=%s",
                        project_id,
                        type(exc).__name__,
                    )
                    latest_job = None
        finally:
            await session.close()

        try:
            config = await _effective_config()
        except AppConfigSnapshotUnavailable:
            # Keep the endpoint available to operators while explicitly
            # signalling that route details are unavailable.  The diagnostics
            # builder omits provider/model and secret-bearing fields for this
            # sentinel instead of inferring a stale startup route.
            config = AppConfigSnapshotUnavailable()
        return build_project_overview_diagnostics(
            config,
            project_id=project_id,
            overview=overview,
            latest_job=latest_job,
        )

    return router


__all__ = [
    "ProjectOverviewMemoryRef",
    "ProjectOverviewDiagnosticsResponse",
    "ProjectOverviewRefreshResponse",
    "ProjectOverviewResponse",
    "create_project_overview_router",
]
