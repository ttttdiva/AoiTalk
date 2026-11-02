"""Authenticated graph persistence API for the external Agent Team canvas."""
from __future__ import annotations

import inspect
from dataclasses import asdict
from typing import Any
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .agent_identity_routes import _resolve_user, _user_field
from ..services.agent_identity_service import AgentIdentityError
from ..services.default_team_graph_service import DefaultTeamGraphService
from ..services.agent_team_graph_schema import GraphDefinition
from ..services.agent_team_graph_service import AgentTeamGraphError, AgentTeamGraphService


class GraphCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    project_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    definition: GraphDefinition


class GraphUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=160)
    project_id: UUID | None = None
    definition: GraphDefinition


class GraphProjection(BaseModel):
    id: UUID
    title: str
    project_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    current_revision_id: UUID
    revision_id: UUID
    version: int
    content_hash: str
    definition: GraphDefinition
    diagnostics: list[str]


class GraphResponse(BaseModel):
    success: bool = True
    graph: GraphProjection


class GraphListResponse(BaseModel):
    success: bool = True
    graphs: list[GraphProjection]
    total: int


class GraphRevisionsResponse(BaseModel):
    success: bool = True
    revisions: list[GraphProjection]
    total: int


class DefaultGraphRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: UUID
    revision_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DefaultGraphUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: UUID
    revision_id: UUID
    expected: DefaultGraphRef | None


def create_agent_team_graph_router(get_db_manager, get_user_from_request, require_auth_dependency):
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    service = AgentTeamGraphService(manager)
    defaults = DefaultTeamGraphService(manager)
    router = APIRouter(prefix="/api/agent-team-graphs", tags=["agent-team-graphs"])

    async def actor(request: Request) -> Any:
        user = await _resolve_user(get_user_from_request, request)
        kind = _user_field(user, "actor_type") or _user_field(user, "principal_kind")
        if bool(_user_field(user, "is_agent", False)) or (kind and str(kind).strip().casefold() != "human"):
            raise HTTPException(status_code=403, detail="administrator human authorization required")
        if str(_user_field(user, "role", "") or "").strip().casefold() != "admin":
            raise HTTPException(status_code=403, detail="administrator authorization required")
        actor_id = _user_field(user, "id") or _user_field(user, "user_id")
        if actor_id in (None, ""):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return actor_id

    async def invoke(method, *args, **kwargs):
        try:
            result = method(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except (AgentTeamGraphError, AgentIdentityError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=getattr(exc, "message", str(exc))) from exc

    @router.post("", response_model=GraphResponse, status_code=201)
    async def create(payload: GraphCreateRequest, request: Request,
                     header_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=255),
                     _: Any = Depends(require_auth_dependency)):
        actor_id = await actor(request)
        if payload.idempotency_key and header_key and payload.idempotency_key != header_key:
            raise HTTPException(status_code=409, detail="idempotency key header/body mismatch")
        key = payload.idempotency_key or header_key
        if not key:
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        graph = await invoke(service.create, actor_user_id=actor_id, title=payload.title,
                             project_id=payload.project_id, idempotency_key=key, definition=payload.definition)
        return {"graph": graph}

    @router.get("", response_model=GraphListResponse)
    async def list_graphs(request: Request, include_archived: bool = False, _: Any = Depends(require_auth_dependency)):
        graphs = await invoke(service.list, actor_user_id=await actor(request), include_archived=include_archived)
        return {"graphs": graphs, "total": len(graphs)}

    @router.get("/default/configuration")
    async def get_default(request: Request, _: Any = Depends(require_auth_dependency)):
        context = await invoke(defaults.get, owner_user_id=await actor(request))
        return {"available": context is not None, "configuration": asdict(context) if context else None}

    @router.put("/default/configuration")
    async def put_default(payload: DefaultGraphUpdate, request: Request, _: Any = Depends(require_auth_dependency)):
        context = await invoke(defaults.set, owner_user_id=await actor(request), graph_id=payload.graph_id,
                               revision_id=payload.revision_id,
                               expected=payload.expected.model_dump(mode="json") if payload.expected else None)
        return {"available": True, "configuration": asdict(context)}

    @router.get("/{graph_id}", response_model=GraphResponse)
    async def get_graph(graph_id: UUID, request: Request, revision_id: UUID | None = None,
                        _: Any = Depends(require_auth_dependency)):
        graph = await invoke(service.get, graph_id, actor_user_id=await actor(request), revision_id=revision_id)
        return {"graph": graph}

    @router.get("/{graph_id}/revisions", response_model=GraphRevisionsResponse)
    async def revisions(graph_id: UUID, request: Request, _: Any = Depends(require_auth_dependency)):
        values = await invoke(service.revisions, graph_id, actor_user_id=await actor(request))
        return {"revisions": values, "total": len(values)}

    @router.post("/{graph_id}/archive", response_model=GraphResponse)
    async def archive(graph_id: UUID, request: Request, _: Any = Depends(require_auth_dependency)):
        graph = await invoke(service.set_archived, graph_id, actor_user_id=await actor(request), archived=True)
        return {"graph": graph}

    @router.post("/{graph_id}/restore", response_model=GraphResponse)
    async def restore(graph_id: UUID, request: Request, _: Any = Depends(require_auth_dependency)):
        graph = await invoke(service.set_archived, graph_id, actor_user_id=await actor(request), archived=False)
        return {"graph": graph}

    @router.put("/{graph_id}", response_model=GraphResponse)
    async def update(graph_id: UUID, payload: GraphUpdateRequest, request: Request,
                     _: Any = Depends(require_auth_dependency)):
        graph = await invoke(service.update, graph_id, actor_user_id=await actor(request),
                             expected_revision=payload.expected_revision, title=payload.title,
                             project_id=payload.project_id, definition=payload.definition)
        return {"graph": graph}

    return router
