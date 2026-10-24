"""Authenticated read/maintenance routes for the common AgentWork ledger."""

from __future__ import annotations

from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, constr
from sqlalchemy import desc, select

from ..features import Features
from ..memory.models import (
    AgentWorkEvent,
    AgentWorkItem,
    ConversationParticipant,
    ConversationSession,
    Project,
    ProjectMember,
)
from ..services.project_permissions import normalize_project_member_permissions
from ..services.agent_work_runtime import AgentWorkCoordinator, InteractiveHistoryFenceError


class InteractiveHistoryFenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    idempotency_key: constr(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    acknowledgment: Literal["provider_outcome_unknown_no_replay"]
    expected_state_digest: constr(pattern=r"^[0-9a-f]{64}$") | None = None
    dry_run: StrictBool = False


async def _maybe(value: Any) -> Any:
    import inspect

    return await value if inspect.isawaitable(value) else value


def create_agent_work_router(
    *,
    get_db_manager: Callable[[], Any],
    require_auth_dependency: Callable[..., Any],
    is_admin_user: Callable[..., Any] | None = None,
    get_coordinator: Callable[[], Any] | None = None,
    get_user_from_request: Callable[..., Any] | None = None,
    get_generation_status: Callable[[str], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/agent-work", tags=["agent-work"])

    async def _session() -> Any:
        manager = get_db_manager() if callable(get_db_manager) else get_db_manager
        manager = await _maybe(manager)
        if manager is None or not callable(getattr(manager, "get_session", None)):
            raise HTTPException(status_code=503, detail="database is unavailable")
        return await _maybe(manager.get_session())

    async def _admin(request: Request) -> None:
        if not Features.autonomous_agent_runtime():
            raise HTTPException(status_code=409, detail="autonomous_agent_runtime is disabled")
        if is_admin_user is None or not bool(await _maybe(is_admin_user(request))):
            raise HTTPException(status_code=403, detail="Administrator privileges required")

    async def _viewer(request: Request, row: Any) -> Any:
        if is_admin_user is not None and bool(await _maybe(is_admin_user(request))):
            return None
        if get_user_from_request is None:
            raise HTTPException(status_code=403, detail="WorkItem access denied")
        user = await _maybe(get_user_from_request(request))
        user_id = getattr(user, "id", None) if user is not None else None
        if isinstance(user, dict):
            user_id = user.get("id") or user.get("user_id")
        session_id = getattr(row, "conversation_session_id", None)
        if session_id is None:
            metadata = getattr(row, "metadata_json", None)
            session_id = metadata.get("conversation_session_id") if isinstance(metadata, dict) else None
        if user_id is None or (row.project_id is None and session_id is None):
            raise HTTPException(status_code=403, detail="WorkItem access denied")
        session = await _session()
        try:
            session_id = getattr(row, "conversation_session_id", None)
            if session_id is None:
                metadata = getattr(row, "metadata_json", None)
                session_id = metadata.get("conversation_session_id") if isinstance(metadata, dict) else None
            if session_id is not None:
                conversation = await session.get(ConversationSession, session_id)
                if conversation is None or getattr(conversation, "deleted_at", None) is not None:
                    raise HTTPException(status_code=403, detail="WorkItem access denied")
                is_owner = str(getattr(conversation, "user_id", "")) == str(user_id)
                if not is_owner:
                    participant = (
                        await session.execute(
                            select(ConversationParticipant).where(
                                ConversationParticipant.session_id == session_id,
                                ConversationParticipant.participant_type == "user",
                                ConversationParticipant.participant_id == str(user_id),
                                ConversationParticipant.status == "joined",
                            )
                        )
                    ).scalars().first()
                    if participant is None:
                        raise HTTPException(status_code=403, detail="WorkItem access denied")
            if row.project_id is None:
                return user
            # A session participant is only one half of the intersection. A
            # project-bound WorkItem also requires current human Project read
            # access, even when the same user owns the session.
            project = await session.get(Project, row.project_id)
            if project is None or getattr(project, "deleted_at", None) is not None:
                raise HTTPException(status_code=403, detail="WorkItem access denied")
            if str(project.owner_id) == str(user_id):
                return user
            member = (await session.execute(select(ProjectMember).where(ProjectMember.project_id == row.project_id, ProjectMember.user_id == user_id))).scalars().first()
            if member is None or normalize_project_member_permissions(getattr(member, "permissions", None)).get("read") is not True:
                raise HTTPException(status_code=403, detail="WorkItem access denied")
            return user
        finally:
            await session.close()

    async def _coordinator() -> AgentWorkCoordinator:
        """Resolve the shared coordinator, with a safe test/rolling fallback."""

        if get_coordinator is not None:
            candidate = await _maybe(get_coordinator())
            if candidate is not None:
                return candidate
        manager = await _maybe(get_db_manager() if callable(get_db_manager) else get_db_manager)
        if manager is None:
            raise HTTPException(status_code=503, detail="AgentWork coordinator is unavailable")
        return AgentWorkCoordinator(manager)

    @router.get("")
    async def list_work(
        request: Request,
        state: str | None = Query(default=None, max_length=32),
        domain: str | None = Query(default=None, max_length=64),
        project_id: str | None = None,
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        for value, label in ((project_id, "project_id"), (agent_id, "agent_id")):
            if value:
                try:
                    UUID(str(value))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422, detail=f"{label} is invalid")
        session = await _session()
        try:
            stmt = select(AgentWorkItem)
            if state:
                stmt = stmt.where(AgentWorkItem.state == state)
            if domain:
                stmt = stmt.where(AgentWorkItem.domain == domain)
            if project_id:
                stmt = stmt.where(AgentWorkItem.project_id == project_id)
            if agent_id:
                stmt = stmt.where(AgentWorkItem.assigned_agent_id == agent_id)
            rows = (await session.execute(stmt.order_by(desc(AgentWorkItem.updated_at)).limit(limit))).scalars().all()
            if is_admin_user is None or not bool(await _maybe(is_admin_user(request))):
                visible: list[Any] = []
                for row in rows:
                    try:
                        await _viewer(request, row)
                    except HTTPException:
                        continue
                    visible.append(row)
                rows = visible
            return JSONResponse({"success": True, "items": [row.to_safe_dict() for row in rows]})
        finally:
            await session.close()

    @router.get("/{work_item_id}")
    async def get_work(
        work_item_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        try:
            UUID(str(work_item_id))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="work_item_id is invalid")
        session = await _session()
        try:
            row = await session.get(AgentWorkItem, work_item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="WorkItem not found")
            await _viewer(request, row)
            events = (await session.execute(select(AgentWorkEvent).where(AgentWorkEvent.work_item_id == row.id).order_by(AgentWorkEvent.sequence))).scalars().all()
            payload = row.to_safe_dict(include_events=False)
            payload["events"] = [event.to_safe_dict() for event in events]
            return JSONResponse({"success": True, "item": payload})
        finally:
            await session.close()

    @router.post("/discover")
    async def discover(request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await _admin(request)
        coordinator = await _coordinator()
        rows = await coordinator.discover_and_materialize()
        return JSONResponse({"success": True, "items": rows})

    @router.post("/tick")
    async def tick(request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await _admin(request)
        coordinator = await _coordinator()
        result = await coordinator.execute_once(limit=coordinator.max_concurrency)
        return JSONResponse({"success": True, "results": result})

    @router.post("/{work_item_id}/cancel")
    async def cancel(work_item_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await _admin(request)
        coordinator = await _coordinator()
        user = await _maybe(get_user_from_request(request)) if get_user_from_request is not None else None
        user_id = getattr(user, "id", None) if user is not None else None
        if isinstance(user, dict):
            user_id = user.get("id") or user.get("user_id")
        cancelled = await coordinator.cancel(work_item_id, actor=str(user_id) if user_id else None)
        if not cancelled:
            raise HTTPException(status_code=404, detail="WorkItem not found or already terminal")
        return JSONResponse({"success": True, "cancelled": True})

    @router.post("/{work_item_id}/history-fence")
    async def history_fence(
        work_item_id: str,
        payload: InteractiveHistoryFenceRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        if not Features.autonomous_agent_runtime():
            raise HTTPException(status_code=409, detail="autonomous_agent_runtime is disabled")
        try:
            UUID(str(work_item_id))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="history_fence_invalid_work_item_id")
        user = await _maybe(get_user_from_request(request)) if get_user_from_request is not None else None
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
        if user_id is None:
            raise HTTPException(status_code=401, detail="history_fence_auth_required")
        if not payload.dry_run and payload.expected_state_digest is None:
            raise HTTPException(status_code=422, detail="history_fence_state_digest_required")
        live_generation_active: bool | None = None
        if get_generation_status is not None:
            try:
                generation_status = await _maybe(get_generation_status(str(payload.session_id)))
            except Exception:
                # An unavailable live-generation store must never assert idle.
                generation_status = None
            if isinstance(generation_status, dict) and type(generation_status.get("running")) is bool:
                live_generation_active = generation_status["running"]
        coordinator = await _coordinator()
        try:
            result = await coordinator.fence_interactive_uncertain_history(
                work_item_id=work_item_id,
                session_id=str(payload.session_id),
                owner_user_id=str(user_id),
                idempotency_key=payload.idempotency_key,
                acknowledgment=payload.acknowledgment,
                expected_state_digest=payload.expected_state_digest,
                dry_run=payload.dry_run,
                live_generation_active=live_generation_active,
            )
        except InteractiveHistoryFenceError as error:
            raise HTTPException(status_code=error.http_status, detail=error.code) from error
        return JSONResponse({"success": True, **result})

    @router.post("/{work_item_id}/resume-after-approval")
    async def resume_after_approval(work_item_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await _admin(request)
        coordinator = await _coordinator()
        action_id = request.query_params.get("action_id")
        resumed = await coordinator.resume_after_approval(work_item_id, action_id=action_id)
        if not resumed:
            raise HTTPException(status_code=409, detail="no approved ExternalAction is available")
        return JSONResponse({"success": True, "resumed": True})

    return router


__all__ = ["InteractiveHistoryFenceRequest", "create_agent_work_router"]
