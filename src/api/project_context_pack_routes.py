"""Authenticated ProjectContextPack projection status and rebuild bridge."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..memory.project_repository import ProjectRepository
from ..services.project_context_pack_job_service import (
    enqueue_project_context_pack_rebuild,
    run_project_context_pack_rebuild_job,
)
from ..services.project_context_pack_service import (
    invalidate_project_context_pack,
)
from ..memory.models import ProjectContextPack


class ContextPackRebuildRequest(BaseModel):
    reason: str | None = Field(default="manual_rebuild", max_length=128)


class ContextPackInvalidateRequest(BaseModel):
    reason: str = Field(default="source_changed", min_length=1, max_length=128)


class ContextPackStatusResponse(BaseModel):
    status: str | None = None
    generated_at: str | None = None
    source_digest: str | None = None
    generation_version: int | None = None
    updated_at: str | None = None
    stale: bool = False


class ContextPackRebuildResponse(BaseModel):
    job_id: str
    status: str


class ContextPackInvalidateResponse(BaseModel):
    status: str
    stale: bool
    job_id: str | None = None


def _uuid(value: str | UUID, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field}") from exc


def _permission_name(*, manage_settings: bool = False) -> str:
    return "manage_settings" if manage_settings else "read"


async def _load_project_with_permission(
    session: Any,
    *,
    project_id: UUID,
    actor_id: UUID,
    permission: str,
) -> Any:
    project = await ProjectRepository.get_by_id(session, project_id=project_id)
    if project is None or getattr(project, "deleted_at", None) is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    allowed = await ProjectRepository.has_permission(
        session,
        project_id=project_id,
        user_id=actor_id,
        permission=permission,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Permission denied")
    return project


def create_project_context_pack_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    """Create the ProjectContextPack metadata/status API.

    The body is intentionally never exposed.  Rebuild/invalidation enqueue a
    durable job only after the source transaction has committed.
    """

    router = APIRouter(prefix="/api/projects", tags=["project-context-pack"])

    async def _actor(request: Request) -> UUID:
        user_info = await get_user_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return _uuid(user_info["id"], field="user_id")

    @router.get(
        "/{project_id}/context-pack",
        response_model=ContextPackStatusResponse,
    )
    async def get_context_pack_status(
        project_id: UUID,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        actor_id = await _actor(request)
        manager = get_db_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        async with await manager.get_session() as session:
            await _load_project_with_permission(
                session,
                project_id=project_id,
                actor_id=actor_id,
                permission=_permission_name(),
            )
            pack = await session.scalar(
                select(ProjectContextPack).where(
                    ProjectContextPack.project_id == project_id
                )
            )
            if pack is None:
                return {
                    "status": None,
                    "generated_at": None,
                    "source_digest": None,
                    "generation_version": None,
                    "updated_at": None,
                    "stale": False,
                }
            pack_status = str(getattr(pack, "status", "fresh") or "fresh")
            stale = pack_status in {"stale", "building", "failed"}
            return {
                "status": pack_status,
                "generated_at": (
                    pack.generated_at.isoformat() if pack.generated_at else None
                ),
                "source_digest": pack.source_digest,
                "generation_version": int(pack.generation_version or 1),
                "updated_at": pack.updated_at.isoformat() if pack.updated_at else None,
                "stale": stale,
            }

    @router.post(
        "/{project_id}/context-pack/rebuild",
        response_model=ContextPackRebuildResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def request_context_pack_rebuild(
        project_id: UUID,
        request: Request,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_auth_dependency),
        payload: ContextPackRebuildRequest = ContextPackRebuildRequest(),
    ) -> dict[str, str]:
        actor_id = await _actor(request)
        manager = get_db_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        async with await manager.get_session() as session:
            await _load_project_with_permission(
                session,
                project_id=project_id,
                actor_id=actor_id,
                permission=_permission_name(manage_settings=True),
            )
        try:
            job = await enqueue_project_context_pack_rebuild(
                project_id,
                actor_id,
                payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        job_id = str(job["id"])
        background_tasks.add_task(run_project_context_pack_rebuild_job, job_id)
        return {"job_id": job_id, "status": job["status"]}

    @router.post(
        "/{project_id}/context-pack/invalidate",
        response_model=ContextPackInvalidateResponse,
    )
    async def invalidate_context_pack_bridge(
        project_id: UUID,
        payload: ContextPackInvalidateRequest,
        request: Request,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        actor_id = await _actor(request)
        manager = get_db_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        # This bridge is used by the Next BFF after a canonical mutation.  A
        # project writer is sufficient; readers can never manufacture stale
        # state.  ``manage_settings`` is accepted as a superset of write.
        async with await manager.get_session() as session:
            project = await ProjectRepository.get_by_id(session, project_id=project_id)
            if project is None or getattr(project, "deleted_at", None) is not None:
                raise HTTPException(status_code=404, detail="Project not found")
            write_allowed = await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=actor_id,
                permission="write",
            )
            manage_allowed = await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=actor_id,
                permission="manage_settings",
            )
            if not (write_allowed or manage_allowed):
                raise HTTPException(status_code=403, detail="Permission denied")
            # Some legacy owners expose only manage_settings; preserve that
            # existing ACL behavior while still rejecting read-only members.
            try:
                await invalidate_project_context_pack(
                    session=session,
                    project_id=project_id,
                    reason=payload.reason,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        # Enqueue only after the mutation transaction committed successfully.
        try:
            job = await enqueue_project_context_pack_rebuild(
                project_id,
                actor_id,
                payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        job_id = str(job["id"])
        background_tasks.add_task(run_project_context_pack_rebuild_job, job_id)
        return {
            "status": "stale",
            "stale": True,
            "job_id": job_id,
        }

    return router


__all__ = [
    "ContextPackInvalidateRequest",
    "ContextPackInvalidateResponse",
    "ContextPackRebuildRequest",
    "ContextPackRebuildResponse",
    "ContextPackStatusResponse",
    "create_project_context_pack_router",
]
