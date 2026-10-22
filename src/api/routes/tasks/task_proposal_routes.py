"""Task-scoped access to live planning proposals for linked conversations."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select

from ....memory.models import AgentRun, ConversationSession, TaskReference
from ....services.human_interaction import (
    HumanInteractionKind,
    get_human_interaction_manager,
)
from ....services.task_reference_service import conversation_reference_visible
from ....services.task_management_service import TaskManagementError
from ._shared import TaskRouterContext


_LIVE_RUN_STATUSES = frozenset({"queued", "running", "awaiting_approval"})
_MARKER_RUN_STATUSES = _LIVE_RUN_STATUSES | {"uncertain"}
_MAX_TASK_IDS = 50
_MAX_TASK_ID_LENGTH = 128
_MAX_TASK_PROPOSAL_TITLE_LENGTH = 120
_MAX_TASK_PROPOSAL_SUMMARY_LENGTH = 2000
_MAX_TASK_PROPOSAL_TEXT_LENGTH = 4000


class TaskProposal(BaseModel):
    """Display-safe projection of one pending server-owned plan proposal."""

    id: str
    task_id: str
    session_id: str
    agent_run_id: str
    revision: int
    plan_id: str
    plan_digest: str
    action_digest: str
    material_action_preview_digest: str
    title: str = Field(max_length=_MAX_TASK_PROPOSAL_TITLE_LENGTH)
    summary: str = Field(max_length=_MAX_TASK_PROPOSAL_SUMMARY_LENGTH)
    plan_text: str = Field(max_length=_MAX_TASK_PROPOSAL_TEXT_LENGTH)
    status: Literal["pending"] = "pending"


class TaskProposalsResponse(BaseModel):
    """A successful empty list is distinct from an unavailable transport."""

    success: Literal[True] = True
    proposals: list[TaskProposal]


class TaskProposalDecisionPayload(BaseModel):
    """Immutable binding echo required to consume a proposal."""

    action: Literal["approve", "reject"]
    request_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    agent_run_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("agent_run_id", "run_id"),
    )
    revision: int = Field(
        ge=1,
        validation_alias=AliasChoices("revision", "plan_revision"),
    )
    plan_id: str = Field(min_length=1)
    plan_digest: str = Field(min_length=1)
    action_digest: str = Field(min_length=1)
    material_action_preview_digest: str = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "material_action_preview_digest", "preview_digest"
        ),
    )


class TaskProposalDecisionResponse(BaseModel):
    success: Literal[True] = True
    status: Literal["resolved"] = "resolved"
    proposal_id: str
    action: Literal["approve", "reject"]
    resolution_action: Literal["approve", "cancel"]


def _proposal_title(payload: Mapping[str, Any]) -> str | None:
    """Extract a short honest title from the proposal objective."""

    objective = str(payload.get("objective") or "").strip()
    text = str(payload.get("plan_text") or "").strip()
    if not objective:
        for line in text.splitlines():
            candidate = line.strip()
            if candidate.casefold().startswith("objective:"):
                objective = candidate.split(":", 1)[1].strip()
                break
    if not objective:
        for line in text.splitlines():
            candidate = line.strip().lstrip("#").strip()
            if candidate:
                objective = candidate
                break
    objective = " ".join(objective.split())
    if len(objective) > _MAX_TASK_PROPOSAL_TITLE_LENGTH:
        objective = objective[: _MAX_TASK_PROPOSAL_TITLE_LENGTH - 3].rstrip() + "..."
    normalized_objective = re.sub(r"[\W_]+", "", objective.casefold())
    if normalized_objective in {"taskname", "keywordtriage"}:
        return None
    return objective or None


def _bounded_display_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _proposal_from_pending(
    pending: Mapping[str, Any],
    *,
    task_id: str,
) -> TaskProposal:
    payload = pending.get("payload")
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_binding_invalid",
                "message": "The live plan proposal has no valid binding.",
            },
        )
    binding = payload.get("plan_binding")
    if not isinstance(binding, Mapping):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_binding_invalid",
                "message": "The live plan proposal has no valid binding.",
            },
        )
    try:
        revision = int(binding.get("revision"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_binding_invalid",
                "message": "The live plan proposal revision is invalid.",
            },
        ) from exc
    plan_id = str(binding.get("plan_id") or "").strip()
    plan_digest = str(binding.get("plan_digest") or "").strip()
    action_digest = str(binding.get("action_digest") or "").strip()
    preview_digest = str(
        payload.get("material_action_preview_digest")
        or binding.get("material_action_preview_digest")
        or ""
    ).strip()
    if not plan_id or not plan_digest or not action_digest or not preview_digest:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_binding_invalid",
                "message": "The live plan proposal binding is incomplete.",
            },
        )
    request_id = str(pending.get("request_id") or "").strip()
    session_id = str(pending.get("session_id") or "").strip()
    run_id = str(pending.get("run_id") or "").strip()
    if not request_id or not session_id or not run_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_binding_invalid",
                "message": "The live plan proposal correlation is incomplete.",
            },
        )
    title = _proposal_title(payload)
    if not title:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "proposal_title_invalid",
                "message": "The live plan proposal has no useful objective title.",
            },
        )
    return TaskProposal(
        id=request_id,
        task_id=str(task_id),
        session_id=session_id,
        agent_run_id=run_id,
        revision=revision,
        plan_id=plan_id,
        plan_digest=plan_digest,
        action_digest=action_digest,
        material_action_preview_digest=preview_digest,
        title=title,
        summary=_bounded_display_text(
            payload.get("summary"), limit=_MAX_TASK_PROPOSAL_SUMMARY_LENGTH
        ),
        plan_text=_bounded_display_text(
            payload.get("plan_text"), limit=_MAX_TASK_PROPOSAL_TEXT_LENGTH
        ),
    )


async def _linked_conversation_session_ids(
    session: Any,
    *,
    task: Any,
    user_id: UUID,
) -> set[str]:
    result = await session.execute(
        select(TaskReference).where(
            TaskReference.task_id == task.id,
            TaskReference.project_id == task.project_id,
            TaskReference.reference_type == "conversation_session",
        )
    )
    session_ids: set[str] = set()
    for reference in result.scalars().all():
        try:
            session_uuid = UUID(str(reference.target_id))
        except (TypeError, ValueError):
            continue
        conversation = await session.get(ConversationSession, session_uuid)
        if await conversation_reference_visible(
            session,
            conversation,
            user_id=user_id,
            project_id=task.project_id,
        ):
            session_ids.add(str(session_uuid))
    return session_ids


async def _live_pending_for_task(
    session: Any,
    *,
    manager: Any,
    task: Any,
    user_id: UUID,
) -> list[dict[str, Any]]:
    linked_sessions = await _linked_conversation_session_ids(
        session,
        task=task,
        user_id=user_id,
    )
    candidates: list[dict[str, Any]] = []
    for session_id in sorted(linked_sessions):
        pending_items = manager.list_pending_plan_approvals(
            user_id=str(user_id),
            session_id=session_id,
        )
        for pending in pending_items:
            run_id = str(pending.get("run_id") or "").strip()
            try:
                run_uuid = UUID(run_id)
            except (TypeError, ValueError):
                continue
            run = await session.get(AgentRun, run_uuid)
            run_status = str(getattr(run, "status", "") or "").strip().casefold()
            if (
                run is None
                or str(getattr(run, "user_id", "") or "") != str(user_id)
                or str(getattr(run, "session_id", "") or "") != session_id
                or run_status not in _LIVE_RUN_STATUSES
            ):
                continue
            if not await _pending_run_matches_task(
                session,
                task=task,
                run_task_id=getattr(run, "task_id", None),
                session_id=session_id,
            ):
                continue
            candidates.append(pending)
    durable_pending = await _durable_pending_plan_runs(
        session,
        session_ids=linked_sessions,
        user_id=user_id,
    )
    candidate_ids = {
        str(item.get("request_id") or "").strip() for item in candidates
    }
    relevant_durable: list[dict[str, str]] = []
    for item in durable_pending:
        if await _pending_run_matches_task(
            session,
            task=task,
            run_task_id=item.get("task_id"),
            session_id=item.get("session_id", ""),
        ):
            relevant_durable.append(item)
    if relevant_durable and not all(
        item["pending_interaction_id"] in candidate_ids
        for item in relevant_durable
    ):
        _raise_conflict(
            "proposal_uncertain",
            "A durable plan proposal is pending but is not available in this process.",
        )
    return candidates


async def _pending_run_matches_task(
    session: Any,
    *,
    task: Any,
    run_task_id: Any,
    session_id: str,
) -> bool:
    """Bind a live proposal to its originating task when that identity exists.

    Legacy conversational runs may not have an AgentRun task ID. Those are
    safe only when the linked conversation belongs to exactly one task. If it
    is linked to several tasks, fail closed with an explicit uncertain state
    instead of showing or accepting the same proposal from the wrong task.
    """

    normalized_run_task_id = str(run_task_id or "").strip()
    if normalized_run_task_id:
        return normalized_run_task_id == str(task.id)

    result = await session.execute(
        select(TaskReference).where(
            TaskReference.reference_type == "conversation_session",
            TaskReference.target_id == session_id,
        )
    )
    linked_task_ids = {
        str(getattr(reference, "task_id", "") or "").strip()
        for reference in result.scalars().all()
        if getattr(reference, "task_id", None)
    }
    if len(linked_task_ids) > 1:
        _raise_conflict(
            "proposal_uncertain",
            "A live proposal is linked to multiple tasks and cannot be safely associated.",
        )
    return linked_task_ids == {str(task.id)}


async def _durable_pending_plan_runs(
    session: Any,
    *,
    session_ids: set[str],
    user_id: UUID,
) -> list[dict[str, str]]:
    """Find restart-visible markers without treating them as actionable.

    The marker is evidence that a request existed before a restart.  It cannot
    recreate the in-memory Future, so callers surface an explicit conflict
    instead of returning a false empty list or auto-resending a proposal.
    """

    parsed_ids: list[UUID] = []
    for value in session_ids:
        try:
            parsed_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not parsed_ids:
        return []
    result = await session.execute(
        select(AgentRun).where(
            AgentRun.session_id.in_(parsed_ids),
            AgentRun.user_id == str(user_id),
            AgentRun.status.in_(_MARKER_RUN_STATUSES),
        )
    )
    markers: list[dict[str, str]] = []
    for run in result.scalars().all():
        metadata = getattr(run, "run_metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        pending_id = str(metadata.get("pending_interaction_id") or "").strip()
        pending_kind = str(metadata.get("pending_interaction_kind") or "").strip()
        if not pending_id or pending_kind != HumanInteractionKind.PLAN_APPROVAL.value:
            continue
        markers.append(
            {
                "pending_interaction_id": pending_id,
                "session_id": str(getattr(run, "session_id", "") or ""),
                "run_id": str(getattr(run, "id", "") or ""),
                "task_id": str(getattr(run, "task_id", "") or ""),
            }
        )
    return markers


def _raise_unavailable() -> None:
    # 501 is intentionally used because the task UI maps it to an unavailable
    # capability while treating a successful empty list as a valid empty state.
    raise HTTPException(
        status_code=501,
        detail={
            "code": "human_interaction_unavailable",
            "message": "The live human interaction transport is unavailable.",
        },
    )


def _raise_conflict(code: str, message: str) -> None:
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


def register_task_proposal_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    """Register task-scoped reads and decisions over live PLAN_APPROVAL requests."""

    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    get_current_user = ctx.get_current_user
    load_task_for_attachment = ctx.load_task_for_attachment
    translate_service_error = ctx.translate_service_error

    @router.get(
        "/task-proposals",
        response_model=TaskProposalsResponse,
        operation_id="list_task_plan_proposals",
    )
    async def list_task_proposals(
        request: Request,
        task_ids: str = Query(..., min_length=1),
        _auth=Depends(require_auth_dependency),
    ) -> TaskProposalsResponse:
        user_id, _ = await get_current_user(request)
        requested_task_ids = list(
            dict.fromkeys(item.strip() for item in task_ids.split(",") if item.strip())
        )
        if not requested_task_ids:
            return TaskProposalsResponse(proposals=[])
        if len(requested_task_ids) > _MAX_TASK_IDS or any(
            len(item) > _MAX_TASK_ID_LENGTH for item in requested_task_ids
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "task_ids_invalid",
                    "message": "At most 50 task IDs of bounded length may be requested.",
                },
            )
        db_session = await get_db_manager().get_session()
        try:
            manager = get_human_interaction_manager()
            if manager is None:
                _raise_unavailable()
            proposals: list[TaskProposal] = []
            for task_id in requested_task_ids:
                try:
                    task = await load_task_for_attachment(
                        db_session,
                        user_id=user_id,
                        task_id=task_id,
                        permission="read",
                    )
                except TaskManagementError as exc:
                    # Batch reads omit inaccessible tasks so a caller cannot
                    # turn the list endpoint into an ACL oracle.
                    if exc.status_code in {400, 403, 404}:
                        continue
                    raise
                pending = await _live_pending_for_task(
                    db_session,
                    manager=manager,
                    task=task,
                    user_id=user_id,
                )
                proposals.extend(
                    _proposal_from_pending(item, task_id=str(task.id))
                    for item in pending
                )
            return TaskProposalsResponse(proposals=proposals)
        finally:
            await db_session.close()

    @router.post(
        "/tasks/{task_id}/agent-proposals/{proposal_id}/decision",
        response_model=TaskProposalDecisionResponse,
        operation_id="decide_task_plan_proposal",
    )
    async def decide_task_proposal(
        task_id: str,
        proposal_id: str,
        payload: TaskProposalDecisionPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ) -> TaskProposalDecisionResponse:
        user_id, _ = await get_current_user(request)
        db_session = await get_db_manager().get_session()
        try:
            task = await load_task_for_attachment(
                db_session,
                user_id=user_id,
                task_id=task_id,
                permission="write",
            )
            manager = get_human_interaction_manager()
            if manager is None:
                _raise_unavailable()
            linked_sessions = await _linked_conversation_session_ids(
                db_session,
                task=task,
                user_id=user_id,
            )
            pending = []
            for session_id in sorted(linked_sessions):
                pending.extend(
                    item
                    for item in manager.list_pending_plan_approvals(
                        user_id=str(user_id),
                        session_id=session_id,
                    )
                    if str(item.get("request_id") or "") == proposal_id
                )
            if len(pending) != 1:
                durable_pending = await _durable_pending_plan_runs(
                    db_session,
                    session_ids=linked_sessions,
                    user_id=user_id,
                )
                for item in durable_pending:
                    if await _pending_run_matches_task(
                        db_session,
                        task=task,
                        run_task_id=item.get("task_id"),
                        session_id=item.get("session_id", ""),
                    ):
                        _raise_conflict(
                            "proposal_uncertain",
                            "A durable plan proposal is pending but is not available in this process.",
                        )
                _raise_conflict(
                    "proposal_stale",
                    "The live proposal has changed or was already consumed.",
                )
            current = pending[0]
            run_id = str(current.get("run_id") or "").strip()
            session_id = str(current.get("session_id") or "").strip()
            if payload.request_id and payload.request_id != proposal_id:
                _raise_conflict(
                    "proposal_stale",
                    "The proposal request ID does not match the requested proposal.",
                )
            if payload.session_id and payload.session_id != session_id:
                _raise_conflict(
                    "proposal_task_mismatch",
                    "The proposal session does not match the live request.",
                )
            if payload.agent_run_id and payload.agent_run_id != run_id:
                _raise_conflict(
                    "proposal_stale",
                    "The proposal run does not match the live request.",
                )
            try:
                run_uuid = UUID(run_id)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "proposal_stale",
                        "message": "The proposal run is no longer valid.",
                    },
                ) from exc
            run = await db_session.get(AgentRun, run_uuid)
            run_status = str(getattr(run, "status", "") or "").strip().casefold()
            if (
                run is None
                or str(getattr(run, "user_id", "") or "") != str(user_id)
                or str(getattr(run, "session_id", "") or "") != session_id
                or run_status not in _LIVE_RUN_STATUSES
            ):
                _raise_conflict(
                    "proposal_stale",
                    "The proposal run is no longer live.",
                )
            if not await _pending_run_matches_task(
                db_session,
                task=task,
                run_task_id=getattr(run, "task_id", None),
                session_id=session_id,
            ):
                _raise_conflict(
                    "proposal_task_mismatch",
                    "The live proposal belongs to a different task.",
                )
            result = manager.resolve_plan_approval(
                proposal_id,
                decision=payload.action,
                requester_user_id=str(user_id),
                requester_session_id=session_id,
                run_id=run_id,
                plan_id=payload.plan_id,
                revision=payload.revision,
                plan_digest=payload.plan_digest,
                action_digest=payload.action_digest,
                material_action_preview_digest=payload.material_action_preview_digest,
            )
            if result is None:
                _raise_conflict(
                    "proposal_stale",
                    "The live proposal has changed or was already consumed.",
                )
            resolution = result.get("resolution")
            resolution_action = (
                str(resolution.get("action") or "")
                if isinstance(resolution, Mapping)
                else ""
            )
            if resolution_action not in {"approve", "cancel"}:
                _raise_conflict(
                    "proposal_stale",
                    "The live proposal resolution was not accepted.",
                )
            return TaskProposalDecisionResponse(
                proposal_id=proposal_id,
                action=payload.action,
                resolution_action=resolution_action,
            )
        except TaskManagementError as exc:
            raise translate_service_error(exc)
        finally:
            await db_session.close()
