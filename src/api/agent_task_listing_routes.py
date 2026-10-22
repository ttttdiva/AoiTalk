"""Owner-scoped read-only listing of persisted Agent task invocations."""

from __future__ import annotations

import inspect
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .operations_routes import _authenticated_human_actor
from ..services.agent_task_listing import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    AgentTaskListingError,
    list_agent_tasks,
)


class AgentTaskRoute(BaseModel):
    provider: str
    model: str
    effort: str


class AgentTaskListItem(BaseModel):
    task_id: str
    work_item_id: UUID
    agent_run_id: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    state: str
    status: str
    terminal: bool
    route: AgentTaskRoute
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None


class AgentTaskListResponse(BaseModel):
    items: list[AgentTaskListItem]
    limit: int = Field(..., ge=1, le=MAX_LIMIT)
    next_cursor: str | None = None


def create_agent_task_listing_router(
    *,
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[[Request], Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-task-invocations",
        tags=["agent-tasks"],
        dependencies=[Depends(require_auth_dependency)],
    )

    @router.get("", operation_id="agent_task_list", response_model=AgentTaskListResponse)
    async def list_tasks(
        request: Request,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        cursor: str | None = Query(None, max_length=128),
        status: str | None = Query(None, max_length=32),
    ) -> Any:
        principal = get_user_from_request(request)
        if inspect.isawaitable(principal):
            principal = await principal
        actor = _authenticated_human_actor(principal)
        authority = (
            principal.get("_authority_source")
            if isinstance(principal, dict)
            else getattr(principal, "_authority_source", None)
        )
        try:
            owner = str(UUID(str(actor.get("id") or actor.get("user_id"))))
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(status_code=403, detail="A stable authenticated human identity is required") from None
        if authority != "web_session" or actor.get("actor_type") != "human" or actor.get("is_agent"):
            raise HTTPException(status_code=403, detail="An ordinary human session is required")
        manager = get_db_manager()
        if inspect.isawaitable(manager):
            manager = await manager
        if manager is None:
            raise HTTPException(status_code=503, detail="Agent task runtime is unavailable")
        try:
            return await list_agent_tasks(manager, owner=owner, limit=limit, cursor=cursor, status=status)
        except AgentTaskListingError as exc:
            return JSONResponse(status_code=exc.http_status, content={"detail": exc.message, "error": exc.code})

    return router
