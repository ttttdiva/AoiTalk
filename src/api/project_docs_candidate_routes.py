"""Review queue API for Project-scoped Docs candidates.

The review queue is deliberately separate from the generic memory decision
routes.  A candidate is a Project resource: readers may list it, while only a
Project ``manage_settings`` actor can apply or reject it.  The service owns
the canonical Docs write and optimistic lifecycle transition; this module is
only the authenticated HTTP boundary.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..services import docs_candidate_service as service


CandidateStatus = Literal["proposed", "approved", "rejected", "superseded"]


class DocsCandidateDTO(BaseModel):
    """Body-safe candidate metadata.

    ``evidence_span`` and any raw transcript are intentionally absent.  The
    service has already sanitized the structured ``content`` payload before
    it reaches this boundary.
    """

    id: str
    project_id: str
    target_node_id: str | None = None
    source_type: str
    content: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    importance: int
    sensitivity: str
    evidence_hash: str | None = None
    has_evidence: bool = False
    source_job_id: str | None = None
    status: CandidateStatus
    version: int
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DocsCandidateListResponse(BaseModel):
    """A bounded Project review queue page."""

    items: list[DocsCandidateDTO] = Field(default_factory=list)
    # ``total`` is the number of rows in this bounded page.  The service API
    # intentionally stays list-shaped for existing workers; callers can omit
    # it in a future count-aware implementation without changing ``items``.
    total: int | None = None


class DocsCandidateApproveRequest(BaseModel):
    """Optimistic approval request."""

    version: int = Field(..., gt=0)
    target_node_id: UUID | None = None


class DocsCandidateRejectRequest(BaseModel):
    """Optimistic rejection request."""

    version: int = Field(..., gt=0)
    reason: str | None = Field(default=None, max_length=1000)


def _compact_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Project the service result to the public, non-transcript DTO."""

    allowed = {
        "id",
        "project_id",
        "target_node_id",
        "source_type",
        "content",
        "confidence",
        "importance",
        "sensitivity",
        "evidence_hash",
        "has_evidence",
        "source_job_id",
        "status",
        "version",
        "created_by",
        "created_at",
        "updated_at",
    }
    return {key: item.get(key) for key in allowed if key in item}


def _raise_http_error(exc: Exception) -> None:
    """Translate service boundary errors without leaking implementation data."""

    if isinstance(exc, service.DocsCandidateConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, service.DocsCandidateNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, service.DocsCandidateValidation):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def create_project_docs_candidate_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    """Create the authenticated Project Docs candidate router.

    ``get_db_manager`` is accepted for parity with the Project router and for
    server registration consistency.  Candidate service operations use the
    shared session factory so approval and its canonical Docs write remain a
    single transaction.
    """

    # Keep the injected manager in the factory signature for parity with the
    # other Project routers.  The service's shared session factory is what
    # preserves approval + canonical Docs mutation in one transaction.
    _ = get_db_manager
    router = APIRouter(prefix="/api/projects", tags=["project-docs-candidates"])

    async def _actor(request: Request) -> dict[str, Any]:
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user_info

    @router.get(
        "/{project_id}/docs-candidates",
        response_model=DocsCandidateListResponse,
    )
    async def list_project_docs_candidates(
        project_id: UUID,
        request: Request,
        status: CandidateStatus | None = Query(default="proposed"),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            rows = await service.list_candidates(
                project_id=project_id,
                actor_user_id=actor["id"],
                status=status,
                limit=limit,
                offset=offset,
            )
            items = [_compact_candidate(row) for row in rows]
            return {"items": items, "total": len(items)}
        except HTTPException:
            raise
        except Exception as exc:
            _raise_http_error(exc)

    @router.post(
        "/{project_id}/docs-candidates/{candidate_id}/approve",
        response_model=DocsCandidateDTO,
    )
    async def approve_project_docs_candidate(
        project_id: UUID,
        candidate_id: UUID,
        payload: DocsCandidateApproveRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            result = await service.approve_candidate(
                candidate_id,
                actor_user_id=actor["id"],
                expected_version=payload.version,
                target_node_id=payload.target_node_id,
                expected_project_id=project_id,
            )
            return _compact_candidate(result)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_http_error(exc)

    @router.post(
        "/{project_id}/docs-candidates/{candidate_id}/reject",
        response_model=DocsCandidateDTO,
    )
    async def reject_project_docs_candidate(
        project_id: UUID,
        candidate_id: UUID,
        payload: DocsCandidateRejectRequest,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            result = await service.reject_candidate(
                candidate_id,
                actor_user_id=actor["id"],
                expected_version=payload.version,
                reason=payload.reason,
                expected_project_id=project_id,
            )
            return _compact_candidate(result)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_http_error(exc)

    return router


__all__ = [
    "DocsCandidateApproveRequest",
    "DocsCandidateDTO",
    "DocsCandidateListResponse",
    "DocsCandidateRejectRequest",
    "create_project_docs_candidate_router",
]
