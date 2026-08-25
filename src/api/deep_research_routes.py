"""Deep Research API routes."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..services.deep_research_service import (
    DEFAULT_ENGINES,
    DeepResearchJob,
    DeepResearchManager,
    DeepResearchRequest,
)


class StartDeepResearchPayload(BaseModel):
    query: str = Field(..., min_length=2, max_length=4000)
    mode: str = "detailed"
    max_iterations: int = Field(3, ge=1, le=8)
    questions_per_iteration: int = Field(3, ge=1, le=6)
    max_results_per_query: int = Field(5, ge=1, le=10)
    engines: list[str] = Field(default_factory=lambda: list(DEFAULT_ENGINES))
    include_local_knowledge: bool = False
    project_id: Optional[str] = None


def _job_payload(job: DeepResearchJob, *, include_report: bool = True) -> dict[str, Any]:
    return job.to_dict(include_report=include_report)


async def _authorized_project_id(
    project_id: Any,
    *,
    user_info: dict[str, Any],
) -> Optional[str]:
    """Return a project scope only after checking the caller's read ACL.

    ``project_id`` is supplied by the request body and therefore cannot be
    copied directly into a usage context.  Keep malformed IDs and inaccessible
    projects fail-closed while letting ``ProjectRepository.has_permission``
    retain its global-admin semantics.
    """

    if project_id in (None, ""):
        return None

    try:
        project_uuid = UUID(str(project_id).strip())
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid project id") from exc

    try:
        user_uuid = UUID(str(user_info.get("id") or "").strip())
    except (TypeError, ValueError, AttributeError) as exc:
        # A project scope cannot be authorized for the legacy/default user,
        # even when the request reached this authenticated route.
        raise HTTPException(status_code=403, detail="Project access denied") from exc

    try:
        from ..memory.database import get_database_manager
        from ..memory.project_repository import ProjectRepository

        database = get_database_manager()
        if database is None:
            raise HTTPException(status_code=503, detail="Database not available")
        session = await database.get_session()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database not available") from exc

    try:
        allowed = await ProjectRepository.has_permission(
            session,
            project_id=project_uuid,
            user_id=user_uuid,
            permission="read",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Project access unavailable") from exc
    finally:
        await session.close()

    if not allowed:
        raise HTTPException(status_code=403, detail="Project access denied")
    return str(project_uuid)


def create_deep_research_router(
    *,
    require_auth_dependency,
    get_current_user,
    config: Any,
) -> APIRouter:
    router = APIRouter(prefix="/api/deep-research", tags=["deep-research"])
    manager = DeepResearchManager(config=config)

    async def _current_user_info(request: Request) -> dict[str, Any]:
        user = await get_current_user(request)
        if isinstance(user, dict):
            return user
        return {}

    async def _current_user_id(user: dict[str, Any] = Depends(_current_user_info)) -> str:
        if user:
            return str(user.get("id") or user.get("username") or "default_user")
        return "default_user"

    @router.get("/engines")
    async def list_engines(_: None = Depends(require_auth_dependency)):
        return {"engines": manager.available_engines(), "default": DEFAULT_ENGINES}

    @router.get("/jobs")
    async def list_jobs(
        _: None = Depends(require_auth_dependency),
        limit: int = Query(30, ge=1, le=100),
        user_id: str = Depends(_current_user_id),
    ):
        return {
            "jobs": [
                _job_payload(job, include_report=False)
                for job in manager.list_jobs(user_id=user_id, limit=limit)
            ]
        }

    @router.post("/jobs", status_code=202)
    async def start_job(
        body: StartDeepResearchPayload,
        _: None = Depends(require_auth_dependency),
        user_info: dict[str, Any] = Depends(_current_user_info),
    ):
        user_id = str(user_info.get("id") or user_info.get("username") or "default_user")
        authorized_project_id = await _authorized_project_id(
            body.project_id,
            user_info=user_info,
        )
        job = await manager.start_job(
            DeepResearchRequest(
                query=body.query,
                mode=body.mode,
                max_iterations=body.max_iterations,
                questions_per_iteration=body.questions_per_iteration,
                max_results_per_query=body.max_results_per_query,
                engines=body.engines,
                include_local_knowledge=body.include_local_knowledge,
                project_id=authorized_project_id,
                actor_user_id=str(user_info.get("id")) if user_info.get("id") else None,
                is_admin=user_info.get("role") == "admin",
            ),
            user_id=user_id,
        )
        return _job_payload(job)

    @router.get("/jobs/{job_id}")
    async def get_job(
        job_id: str,
        _: None = Depends(require_auth_dependency),
        user_id: str = Depends(_current_user_id),
    ):
        job = manager.get_job(job_id, user_id=user_id)
        if not job:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        return _job_payload(job)

    @router.get("/jobs/{job_id}/markdown")
    async def export_markdown(
        job_id: str,
        _: None = Depends(require_auth_dependency),
        user_id: str = Depends(_current_user_id),
    ):
        job = manager.get_job(job_id, user_id=user_id)
        if not job:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        return Response(
            content=job.report_markdown or "",
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="deep-research-{job.id}.md"'
            },
        )

    return router
