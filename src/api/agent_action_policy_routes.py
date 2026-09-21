"""Administrator-only immutable action policy management."""
from contextlib import asynccontextmanager
from datetime import datetime
import inspect
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.agent_action_policy_service import AgentActionPolicyService
from ..services.integration_action_registry import ActionPolicyError


class PolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str
    display_name: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=255)


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    state: Literal["draft", "active", "paused", "retired"] | None = None


class PolicyRevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=255)
    action_type: str = Field(min_length=1, max_length=80)
    connection_id: str
    authorization_mode: Literal["human_approval", "bounded_auto"]
    constraints: dict
    rate_limit: dict
    dedupe_window_seconds: int = Field(ge=0, le=31536000, strict=True)
    fallback_behavior: Literal["require_approval", "block"] = "block"
    active_from: datetime | None = None
    active_until: datetime | None = None


async def policy_actor(request, get_user):
    try:
        AgentActionPolicyService.feature_gate()
    except ActionPolicyError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    actor = get_user(request)
    if inspect.isawaitable(actor):
        actor = await actor
    def field(key, default=None):
        return actor.get(key, default) if isinstance(actor, dict) else getattr(actor, key, default)
    if not actor:
        raise HTTPException(401, "authentication_required")
    if field("is_agent", False) or (field("actor_type") or field("principal_kind") or "human") != "human" or field("role") != "admin":
        raise HTTPException(403, "administrator_human_required")
    return field("id") or field("user_id")


@asynccontextmanager
async def policy_session(get_db):
    manager = get_db() if callable(get_db) else get_db
    session = manager.get_session()
    if inspect.isawaitable(session):
        session = await session
    try:
        yield session
        await session.commit()
    except ActionPolicyError as exc:
        await session.rollback()
        raise HTTPException(exc.status_code, exc.code) from None
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def create_agent_action_policy_router(get_db_manager, get_user_from_request, require_auth_dependency,
                                      *, policy_service=None, config=None):
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    service = policy_service or AgentActionPolicyService(manager, config=config)
    router = APIRouter(prefix="/api/agent-action-policies", tags=["agent-action-policies"], dependencies=[Depends(require_auth_dependency)])

    @router.get("")
    async def list_policies(request: Request, agent_id: str):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            rows = await service.list_policies(session, actor_user_id=actor, agent_id=agent_id)
            return {"success": True, "policies": rows, "total": len(rows)}

    @router.post("", status_code=201)
    async def create_policy(request: Request, payload: PolicyCreate):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            return {"success": True, "policy": await service.create_policy(session, actor_user_id=actor, **payload.model_dump())}

    @router.get("/{policy_id}")
    async def get_policy(request: Request, policy_id: str):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            return {"success": True, "policy": await service.get_policy(session, policy_id, actor_user_id=actor)}

    @router.patch("/{policy_id}")
    async def update_policy(request: Request, policy_id: str, payload: PolicyUpdate):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            return {"success": True, "policy": await service.update_policy(session, policy_id, actor_user_id=actor, **payload.model_dump())}

    @router.post("/{policy_id}/revisions", status_code=201)
    async def create_revision(request: Request, policy_id: str, payload: PolicyRevisionCreate):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            return {"success": True, "policy": await service.create_revision(session, policy_id, actor_user_id=actor, **payload.model_dump())}

    return router
