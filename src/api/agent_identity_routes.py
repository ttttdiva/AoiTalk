"""HTTP routes for the WS01 generic Agent identity foundation.

The route module is intentionally a thin, authenticated boundary around
``AgentIdentityService`` and ``AgentAuthorityResolver``.  It does not accept
an actor id from a request body: mutating calls derive the human administrator
from the server's authenticated user resolver and the service performs the
database-backed role check again.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from ..features import Features
from ..services.agent_authority import AgentAuthorityResolver
from ..services.agent_identity_service import (
    AgentIdentityError,
    AgentIdentityService,
)
from ..memory.models import (
    AgentProjectGrant,
    AgentSpaceAssignment,
    AgentTaskAssignment,
    PersonaOperatorAssignment,
)

logger = logging.getLogger(__name__)


_MAX_MAP_FIELDS = 24
_MAX_LIST_ITEMS = 64
_SECRET_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "cookie",
)


class _CommandModel(BaseModel):
    """Shared strict model configuration for this API boundary."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


def _bounded_map(value: Any) -> Any:
    if value is not None and (not isinstance(value, Mapping) or len(value) > _MAX_MAP_FIELDS):
        raise ValueError("object has too many fields or is not an object")
    return value


class OrganizationPatchRequest(_CommandModel):
    expected_policy_version: int | None = Field(default=None, ge=1)
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("display_name", "name", "company_name"),
    )
    legal_name: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    autonomy_level: Literal["disabled", "supervised", "bounded", "autonomous"] | None = None
    policy: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("policy", "policy_json"),
    )
    budget_policy: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("budget_policy", "budget_policy_json"),
    )

    _policy_limit = field_validator("policy", "budget_policy")(_bounded_map)


class CreateAgentRequest(_CommandModel):
    display_name: str = Field(min_length=1, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=255)
    slug: str | None = Field(default=None, max_length=100)
    character_id: str | None = Field(default=None, max_length=64)


class AgentStateRequest(_CommandModel):
    expected_state: Literal["draft", "active", "paused", "retired"] | None = None
    state: Literal["draft", "active", "paused", "retired"] = Field(
        validation_alias=AliasChoices("state", "target_state")
    )


class CreateRevisionRequest(_CommandModel):
    display_name: str = Field(min_length=1, max_length=160)
    mission: str = Field(default="", max_length=10_000)
    responsibility_summary: str = Field(default="", max_length=10_000)
    operational_instructions: str = Field(default="", max_length=30_000)
    agent_team_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("agent_team_id", "team_id"),
    )
    execution_profile_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("execution_profile_id", "execution_profile"),
    )
    allowed_subagent_ids: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    capability_ceiling: list[str] = Field(
        default_factory=list,
        max_length=_MAX_LIST_ITEMS,
        validation_alias=AliasChoices("capability_ceiling", "capability_ceiling_json"),
    )
    wake_policy: dict[str, Any] | None = None
    budget_policy: dict[str, Any] | None = None
    concurrency_policy: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    version: int | None = Field(default=None, ge=1, le=2_000_000_000)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)

    _policy_limit = field_validator("wake_policy", "budget_policy", "concurrency_policy")(_bounded_map)


class OrganizationProfilePatchRequest(_CommandModel):
    job_title: str | None = Field(default=None, max_length=160)
    responsibility_summary: str | None = Field(default=None, max_length=10_000)
    primary_space_id: str | None = Field(default=None, max_length=64)
    manager_user_id: str | None = Field(default=None, max_length=64)
    manager_agent_id: str | None = Field(default=None, max_length=64)
    autonomy_level: Literal["disabled", "supervised", "bounded", "autonomous"] | None = None
    company_permission_ceiling: dict[str, Any] | None = None
    employment_state: Literal["active", "on_leave", "suspended", "terminated", "contractor"] | None = None

    _policy_limit = field_validator("company_permission_ceiling")(_bounded_map)


class SpaceAssignmentRequest(_CommandModel):
    space_id: str = Field(min_length=1, max_length=64)
    assignment_kind: Literal["primary", "secondary", "supporting"] = "supporting"
    role_label: str | None = Field(default=None, max_length=120)
    policy_ceiling: dict[str, Any] | None = None
    active_from: datetime | None = None
    active_until: datetime | None = None

    _policy_limit = field_validator("policy_ceiling")(_bounded_map)


class ProjectGrantRequest(_CommandModel):
    project_id: str = Field(min_length=1, max_length=64)
    role: Literal["owner", "admin", "member", "viewer"] = "viewer"
    permissions: dict[str, bool] | None = None
    active_from: datetime | None = None
    active_until: datetime | None = None

    _permission_limit = field_validator("permissions")(_bounded_map)


class TaskAssignmentRequest(_CommandModel):
    task_id: str = Field(min_length=1, max_length=64)
    assignment_role: Literal["owner", "executor", "reviewer", "observer"] = Field(
        default="executor",
        validation_alias=AliasChoices("assignment_role", "role"),
    )
    active_from: datetime | None = None
    active_until: datetime | None = None


class PersonaOperatorAssignmentRequest(_CommandModel):
    persona_id: str = Field(min_length=1, max_length=64)
    role: Literal["operator", "strategist", "researcher", "creator", "analyst", "publisher"] = "operator"
    is_primary: bool = False
    capability_ceiling: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    active_from: datetime | None = None
    active_until: datetime | None = None


class AssignmentStateRequest(_CommandModel):
    state: Literal["active", "revoked", "expired"] = Field(
        validation_alias=AliasChoices("state", "target_state")
    )


def _dump(
    payload: BaseModel,
    *,
    exclude_unset: bool = False,
    include_none: bool = False,
) -> dict[str, Any]:
    """Dump a validated request without allowing aliases to become authority."""

    return payload.model_dump(
        mode="python",
        exclude_none=not include_none,
        exclude_unset=exclude_unset,
    )


def _json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Project service DTOs into bounded JSON and omit secret-shaped keys."""

    if _depth > 8:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (UUID, datetime)):
        return value.isoformat()
    if hasattr(value, "to_safe_dict") and callable(value.to_safe_dict):
        return _json_safe(value.to_safe_dict(), _depth=_depth + 1)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict(), _depth=_depth + 1)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            key_folded = key.casefold()
            if any(marker in key_folded for marker in _SECRET_MARKERS):
                continue
            result[key] = _json_safe(raw_value, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, _depth=_depth + 1) for item in list(value)[:500]]
    # Service DTOs should never reach this branch.  Avoid serializing arbitrary
    # objects (which could expose ORM internals or provider credentials).
    return None


def _response(key: str, value: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content={"success": True, key: _json_safe(value)},
        status_code=status_code,
    )


def _list_response(key: str, values: Any) -> JSONResponse:
    items = values if isinstance(values, (list, tuple)) else []
    safe = _json_safe(list(items))
    return JSONResponse(content={"success": True, key: safe, "total": len(items)})


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, AgentIdentityError):
        return HTTPException(status_code=int(exc.status_code), detail=exc.message)
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(status_code=422, detail=str(exc) or "invalid request")
    logger.exception("Agent identity API operation failed", exc_info=exc)
    return HTTPException(status_code=500, detail="Agent identity operation failed")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _resolve_user(get_user_from_request: Callable[..., Any], request: Request) -> Any:
    try:
        user = await _maybe_await(get_user_from_request(request))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Not authenticated") from exc
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _user_field(user: Any, name: str, default: Any = None) -> Any:
    if isinstance(user, Mapping):
        return user.get(name, default)
    return getattr(user, name, default)


def create_agent_identity_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    config=None,
) -> APIRouter:
    """Build WS01 Organization, Agent, assignment, and authority routes."""

    # ``get_db_manager`` is a callable in WebChatServer but accepting an
    # already-created manager keeps this factory easy to exercise in tests.
    try:
        db_manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    except Exception:
        db_manager = None

    identity = AgentIdentityService(db_manager, config=config)
    authority = AgentAuthorityResolver(db_manager, config=config)
    router = APIRouter(prefix="/api", tags=["agent-identity"])

    async def current_user(request: Request) -> Any:
        return await _resolve_user(get_user_from_request, request)

    async def admin_actor(request: Request) -> Any:
        user = await current_user(request)
        actor_kind = _user_field(user, "actor_type") or _user_field(user, "principal_kind")
        if bool(_user_field(user, "is_agent", False)):
            raise HTTPException(status_code=403, detail="administrator human authorization required")
        if actor_kind and str(actor_kind).strip().casefold() not in {"human"}:
            raise HTTPException(status_code=403, detail="administrator human authorization required")
        role = str(_user_field(user, "role", "") or "").strip().casefold()
        if role != "admin":
            raise HTTPException(status_code=403, detail="administrator authorization required")
        actor_id = _user_field(user, "id") or _user_field(user, "user_id")
        if actor_id in (None, ""):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return actor_id

    def company_enabled() -> None:
        try:
            flag = getattr(Features, "virtual_company", None)
            enabled = bool(flag() if callable(flag) else flag)
        except Exception:
            enabled = bool(Features.is_enabled("virtual_company"))
        if not enabled:
            # Company-specific surfaces remain undiscoverable in Personal and
            # Enterprise defaults until a future workstream enables the gate.
            raise HTTPException(status_code=404, detail="virtual company features are disabled")

    def assignment_response(kind: str, value: Any, *, status_code: int = 200) -> JSONResponse:
        return _response(kind, value, status_code=status_code)

    async def _invoke(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await _maybe_await(method(*args, **kwargs))
        except Exception as exc:
            raise _service_error(exc) from exc

    # ── Organization ──────────────────────────────────────────────────
    @router.get("/organization")
    async def get_organization(request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await current_user(request)
        return _response("organization", await _invoke(identity.organizations.get))

    @router.put("/organization")
    @router.patch("/organization")
    async def patch_organization(
        payload: OrganizationPatchRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        actor_id = await admin_actor(request)
        return _response(
            "organization",
            await _invoke(
                identity.organizations.update,
                _dump(payload, exclude_unset=True, include_none=True),
                actor_user_id=actor_id,
            ),
        )

    # ── Agent identity and revisions ──────────────────────────────────
    @router.post("/agents", status_code=201)
    async def post_agent(
        payload: CreateAgentRequest,
        request: Request,
        header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        actor_id = await admin_actor(request)
        body = _dump(payload)
        body_key = body.pop("idempotency_key", None)
        if body_key and header_idempotency_key and body_key != header_idempotency_key:
            raise HTTPException(status_code=409, detail="idempotency key header/body mismatch")
        key = body_key or header_idempotency_key
        if not key:
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        body["idempotency_key"] = key
        return _response("agent", await _invoke(identity.create_agent, **body, actor_user_id=actor_id), status_code=201)

    @router.get("/agents")
    async def get_agents(
        request: Request,
        state: str | None = Query(default=None, max_length=16),
        limit: int = Query(default=100, ge=1, le=200),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        return _list_response("agents", await _invoke(identity.list_agents, state=state, limit=limit))

    @router.get("/agents/catalog")
    async def get_employee_catalog(request: Request, _: Any = Depends(require_auth_dependency)) -> dict[str, Any]:
        company_enabled()
        if not Features.autonomous_agent_runtime():
            raise HTTPException(status_code=404, detail="employee runtime is disabled")
        actor_id = await admin_actor(request)
        return await _invoke(identity.employee_catalog, actor_user_id=actor_id)

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await current_user(request)
        result = await _invoke(identity.get_agent, agent_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return _response("agent", result)

    @router.patch("/agents/{agent_id}/state")
    async def patch_agent_state(
        agent_id: str,
        payload: AgentStateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        actor_id = await admin_actor(request)
        return _response("agent", await _invoke(identity.transition_agent, agent_id, payload.state, actor_user_id=actor_id, expected_state=payload.expected_state))

    @router.post("/agents/{agent_id}/revisions", status_code=201)
    async def post_agent_revision(
        agent_id: str,
        payload: CreateRevisionRequest,
        request: Request,
        header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        actor_id = await admin_actor(request)
        body = _dump(payload)
        body_key = body.pop("idempotency_key", None)
        if body_key and header_idempotency_key and body_key != header_idempotency_key:
            raise HTTPException(status_code=409, detail="idempotency key header/body mismatch")
        key = body_key or header_idempotency_key
        if not key:
            raise HTTPException(status_code=422, detail="idempotency_key is required")
        body["idempotency_key"] = key
        return _response("revision", await _invoke(identity.create_revision, agent_id=agent_id, **body, actor_user_id=actor_id), status_code=201)

    @router.get("/agents/{agent_id}/revisions")
    async def get_agent_revisions(
        agent_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        return _list_response("revisions", await _invoke(identity.list_revisions, agent_id, limit=limit))

    @router.get("/agent-revisions/{revision_id}")
    async def get_revision(revision_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await current_user(request)
        result = await _invoke(identity.get_revision, revision_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Agent revision not found")
        return _response("revision", result)

    # ── Company profile ───────────────────────────────────────────────
    @router.get("/agents/{agent_id}/organization-profile")
    async def get_organization_profile(agent_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        await current_user(request)
        company_enabled()
        result = await _invoke(identity.get_organization_profile, agent_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Agent organization profile not found")
        return _response("organization_profile", result)

    @router.patch("/agents/{agent_id}/organization-profile")
    async def patch_organization_profile(
        agent_id: str,
        payload: OrganizationProfilePatchRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return _response(
            "organization_profile",
            await _invoke(
                identity.upsert_organization_profile,
                agent_id=agent_id,
                payload=_dump(payload, exclude_unset=True, include_none=True),
                actor_user_id=actor_id,
            ),
        )

    # ── Explicit assignment relations ────────────────────────────────
    @router.post("/agents/{agent_id}/space-assignments", status_code=201)
    async def post_space_assignment(
        agent_id: str,
        payload: SpaceAssignmentRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("space_assignment", await _invoke(identity.create_space_assignment, agent_id=agent_id, **_dump(payload), actor_user_id=actor_id), status_code=201)

    @router.get("/agents/{agent_id}/space-assignments")
    async def get_space_assignments(
        agent_id: str,
        request: Request,
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=200, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        company_enabled()
        return _list_response("space_assignments", await _invoke(identity.list_assignments, AgentSpaceAssignment, agent_id=agent_id, include_inactive=include_inactive, limit=limit))

    @router.post("/agents/{agent_id}/space-assignments/{assignment_id}/revoke")
    async def revoke_space_assignment(agent_id: str, assignment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("space_assignment", await _invoke(identity.revoke_assignment, AgentSpaceAssignment, assignment_id, actor_user_id=actor_id, agent_id=agent_id))

    @router.patch("/agents/{agent_id}/space-assignments/{assignment_id}/state")
    async def patch_space_assignment_state(agent_id: str, assignment_id: str, payload: AssignmentStateRequest, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("space_assignment", await _invoke(identity.transition_assignment, AgentSpaceAssignment, assignment_id, payload.state, actor_user_id=actor_id, agent_id=agent_id))

    @router.post("/agents/{agent_id}/project-grants", status_code=201)
    async def post_project_grant(
        agent_id: str,
        payload: ProjectGrantRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("project_grant", await _invoke(identity.create_project_grant, agent_id=agent_id, **_dump(payload), actor_user_id=actor_id), status_code=201)

    @router.get("/agents/{agent_id}/project-grants")
    async def get_project_grants(
        agent_id: str,
        request: Request,
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=200, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        company_enabled()
        return _list_response("project_grants", await _invoke(identity.list_assignments, AgentProjectGrant, agent_id=agent_id, include_inactive=include_inactive, limit=limit))

    @router.post("/agents/{agent_id}/project-grants/{assignment_id}/revoke")
    async def revoke_project_grant(agent_id: str, assignment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("project_grant", await _invoke(identity.revoke_assignment, AgentProjectGrant, assignment_id, actor_user_id=actor_id, agent_id=agent_id))

    @router.patch("/agents/{agent_id}/project-grants/{assignment_id}/state")
    async def patch_project_grant_state(agent_id: str, assignment_id: str, payload: AssignmentStateRequest, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("project_grant", await _invoke(identity.transition_assignment, AgentProjectGrant, assignment_id, payload.state, actor_user_id=actor_id, agent_id=agent_id))

    @router.post("/agents/{agent_id}/task-assignments", status_code=201)
    async def post_task_assignment(
        agent_id: str,
        payload: TaskAssignmentRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("task_assignment", await _invoke(identity.create_task_assignment, agent_id=agent_id, **_dump(payload), actor_user_id=actor_id), status_code=201)

    @router.get("/agents/{agent_id}/task-assignments")
    async def get_task_assignments(
        agent_id: str,
        request: Request,
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=200, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        company_enabled()
        return _list_response("task_assignments", await _invoke(identity.list_assignments, AgentTaskAssignment, agent_id=agent_id, include_inactive=include_inactive, limit=limit))

    @router.post("/agents/{agent_id}/task-assignments/{assignment_id}/revoke")
    async def revoke_task_assignment(agent_id: str, assignment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("task_assignment", await _invoke(identity.revoke_assignment, AgentTaskAssignment, assignment_id, actor_user_id=actor_id, agent_id=agent_id))

    @router.patch("/agents/{agent_id}/task-assignments/{assignment_id}/state")
    async def patch_task_assignment_state(agent_id: str, assignment_id: str, payload: AssignmentStateRequest, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("task_assignment", await _invoke(identity.transition_assignment, AgentTaskAssignment, assignment_id, payload.state, actor_user_id=actor_id, agent_id=agent_id))

    @router.post("/agents/{agent_id}/persona-operator-assignments", status_code=201)
    async def post_persona_operator_assignment(
        agent_id: str,
        payload: PersonaOperatorAssignmentRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("persona_operator_assignment", await _invoke(identity.create_persona_operator_assignment, agent_id=agent_id, **_dump(payload), actor_user_id=actor_id), status_code=201)

    @router.get("/agents/{agent_id}/persona-operator-assignments")
    async def get_persona_operator_assignments(
        agent_id: str,
        request: Request,
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=200, ge=1, le=500),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await current_user(request)
        company_enabled()
        return _list_response("persona_operator_assignments", await _invoke(identity.list_assignments, PersonaOperatorAssignment, agent_id=agent_id, include_inactive=include_inactive, limit=limit))

    @router.post("/agents/{agent_id}/persona-operator-assignments/{assignment_id}/revoke")
    async def revoke_persona_operator_assignment(agent_id: str, assignment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("persona_operator_assignment", await _invoke(identity.revoke_assignment, PersonaOperatorAssignment, assignment_id, actor_user_id=actor_id, agent_id=agent_id))

    @router.patch("/agents/{agent_id}/persona-operator-assignments/{assignment_id}/state")
    async def patch_persona_operator_assignment_state(agent_id: str, assignment_id: str, payload: AssignmentStateRequest, request: Request, _: Any = Depends(require_auth_dependency)) -> JSONResponse:
        company_enabled()
        actor_id = await admin_actor(request)
        return assignment_response("persona_operator_assignment", await _invoke(identity.transition_assignment, PersonaOperatorAssignment, assignment_id, payload.state, actor_user_id=actor_id, agent_id=agent_id))

    # ── Effective authority inspection ────────────────────────────────
    @router.get("/agents/{agent_id}/effective-authority")
    @router.get("/agents/{agent_id}/authority")
    async def get_agent_authority(
        agent_id: str,
        request: Request,
        revision_id: str | None = Query(default=None, max_length=64),
        project_id: str | None = Query(default=None, max_length=64),
        space_id: str | None = Query(default=None, max_length=64),
        persona_id: str | None = Query(default=None, max_length=64),
        required_capability: str | None = Query(default=None, max_length=80),
        tool_capabilities: list[str] | None = Query(default=None, max_length=_MAX_LIST_ITEMS),
        harness_capabilities: list[str] | None = Query(default=None, max_length=_MAX_LIST_ITEMS),
        external_action_approved: bool | None = Query(default=None),
        _: Any = Depends(require_auth_dependency),
    ) -> JSONResponse:
        await admin_actor(request)

        def split_capabilities(values: list[str] | None) -> list[str] | None:
            if values is None:
                return None
            result: list[str] = []
            for value in values:
                for item in str(value).split(","):
                    item = item.strip()
                    if item and item not in result:
                        result.append(item)
            return result

        result = await _invoke(
            authority.resolve,
            agent_id=agent_id,
            revision_id=revision_id,
            project_id=project_id,
            space_id=space_id,
            persona_id=persona_id,
            required_capability=required_capability,
            tool_capabilities=split_capabilities(tool_capabilities),
            harness_capabilities=split_capabilities(harness_capabilities),
            external_action_approved=external_action_approved,
        )
        if hasattr(result, "to_dict") and callable(result.to_dict):
            result = result.to_dict()
        return _response("authority", result)

    return router


__all__ = [
    "OrganizationPatchRequest",
    "CreateAgentRequest",
    "AgentStateRequest",
    "CreateRevisionRequest",
    "OrganizationProfilePatchRequest",
    "SpaceAssignmentRequest",
    "ProjectGrantRequest",
    "TaskAssignmentRequest",
    "PersonaOperatorAssignmentRequest",
    "AssignmentStateRequest",
    "create_agent_identity_router",
    "create_agent_router",
]

create_agent_router = create_agent_identity_router
