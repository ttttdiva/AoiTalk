"""Human-only Learning proposal review and Character application routes.

Creation/list/read remain on the metrics router for compatibility.  This
separate router owns only mutating review commands so it cannot accidentally
expose an approval or apply operation through the agent-facing metrics API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.media_operations_learning_service import MediaOperationsLearningService
from .media_operations_routes import _actor, _invoke, _principal_projection, _with_session


class _LearningCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LearningProposalEditRequest(_LearningCommandModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, min_length=1, max_length=4000)
    recommendation: str | None = Field(default=None, min_length=1, max_length=8000)
    evidence_refs: list[dict[str, Any]] | None = Field(default=None, max_length=32)
    human_decision_refs: list[str] | None = Field(default=None, max_length=64)
    target_fields: list[str] | None = Field(default=None, max_length=24)
    proposed_before: dict[str, Any] | None = None
    proposed_after: dict[str, Any] | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: float | None = Field(default=None, ge=0, le=1)
    reason: str = Field(min_length=1, max_length=4000)


class LearningProposalDecisionRequest(_LearningCommandModel):
    reason: str = Field(min_length=1, max_length=4000)
    expected_persona_revision_id: str | None = None
    expected_persona_revision_version: int | None = Field(default=None, ge=1)
    expected_persona_revision_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class LearningProposalApplyRequest(_LearningCommandModel):
    reason: str | None = Field(default=None, max_length=4000)
    expected_persona_revision_id: str | None = None
    expected_persona_revision_version: int | None = Field(default=None, ge=1)
    expected_persona_revision_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


def _dump(payload: BaseModel, *, exclude_unset: bool = False) -> dict[str, Any]:
    return payload.model_dump(mode="json", exclude_none=True, exclude_unset=exclude_unset)


def create_media_operations_learning_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/operations/media/learning-proposals", tags=["operations-media-learning"])
    media = MediaOperationsLearningService()

    async def current_actor(request: Request) -> dict[str, Any]:
        user = await _actor(get_user_from_request, request)
        return _principal_projection(user)

    @router.patch("/{proposal_id}", response_model=dict, operation_id="media_edit_learning_proposal")
    async def edit_learning(proposal_id: str, payload: LearningProposalEditRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        body = _dump(payload, exclude_unset=True)
        reason = body.pop("reason")
        return await _with_session(get_db_manager, lambda session: _invoke(media.edit_learning_proposal, session, actor, proposal_id, changes=body, reason=reason, idempotency_key=idempotency_key))

    @router.post("/{proposal_id}/approve", response_model=dict, operation_id="media_approve_learning_proposal")
    async def approve_learning(proposal_id: str, payload: LearningProposalDecisionRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.approve_learning_proposal, session, actor, proposal_id, **_dump(payload), idempotency_key=idempotency_key))

    @router.post("/{proposal_id}/reject", response_model=dict, operation_id="media_reject_learning_proposal")
    async def reject_learning(proposal_id: str, payload: LearningProposalDecisionRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        body = _dump(payload)
        body.pop("expected_persona_revision_id", None)
        body.pop("expected_persona_revision_version", None)
        body.pop("expected_persona_revision_hash", None)
        return await _with_session(get_db_manager, lambda session: _invoke(media.reject_learning_proposal, session, actor, proposal_id, **body, idempotency_key=idempotency_key))

    @router.post("/{proposal_id}/apply", response_model=dict, operation_id="media_apply_learning_proposal")
    async def apply_learning(proposal_id: str, payload: LearningProposalApplyRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.apply_learning_proposal, session, actor, proposal_id, **_dump(payload), idempotency_key=idempotency_key))

    return router


__all__ = [
    "LearningProposalEditRequest",
    "LearningProposalDecisionRequest",
    "LearningProposalApplyRequest",
    "create_media_operations_learning_router",
]
