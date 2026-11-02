"""Human-admin management APIs for the employee graph and order workflow."""

from __future__ import annotations

import inspect
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent_identity_routes import CreateRevisionRequest
from ..services.agent_delegation_service import AgentDelegationService, DelegationError, MediaInputPinDTO, OperationsSourceRefDTO
from ..services.agent_identity_service import AgentIdentityError
from ..services.agent_organization_service import AgentOrganizationService
from ..services.agent_delegation_planning import DelegationPlanningPolicy, PlannedAssignment, PlanningRosterEntry
from ..services.agent_team_graph_admission import TeamGraphRef


class _SafeDelegationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "delegation_request_invalid") from None
        return safe_handler


class GraphPlanningInput(DelegationPlanningPolicy):
    roster: list[PlanningRosterEntry] | None = Field(default=None, min_length=2, max_length=32)
    graph_constraints: None = None

    @model_validator(mode="after")
    def unique_keys(self):
        return self if self.roster is None else super().unique_keys()


class DelegationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegator_agent_id: UUID
    delegator_revision_id: UUID
    worker_agent_id: UUID | None = None
    worker_revision_id: UUID | None = None
    reviewer_agent_id: UUID | None = None
    reviewer_revision_id: UUID | None = None
    project_id: UUID
    persona_id: UUID | None = None
    parent_delegation_id: UUID | None = None
    linked_work_item_id: UUID | None = None
    title: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=12000)
    work_role: Literal["researcher", "writer", "data_worker", "general_worker", "code_worker"]
    idempotency_key: str = Field(min_length=1, max_length=255)
    planning: DelegationPlanningPolicy | GraphPlanningInput | None = None
    media_input_pins: list[MediaInputPinDTO] | None = Field(default=None, max_length=8)
    operations_source_ref: OperationsSourceRefDTO | None = None
    team_graph_ref: TeamGraphRef | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_graph_fields(cls, value):
        if isinstance(value, dict) and isinstance(value.get("planning"), dict):
            planning = value["planning"]
            if "graph_constraints" in planning or (value.get("team_graph_ref") and "roster" in planning):
                raise ValueError("server controlled graph fields")
        return value

    @model_validator(mode="after")
    def validate_graph_input(self):
        if self.team_graph_ref:
            if self.planning is None or self.planning.roster is not None or self.planning.graph_constraints is not None:
                raise ValueError("invalid graph planning")
        elif any(getattr(self, name) is None for name in ("worker_agent_id", "worker_revision_id", "reviewer_agent_id", "reviewer_revision_id")) or isinstance(self.planning, GraphPlanningInput):
            raise ValueError("delegation identifiers and roster required")
        return self


class DelegationRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_work_item_id: UUID


class DelegationReadiness(BaseModel):
    ready: bool
    error_code: str | None = None


class DelegationArtifactDTO(BaseModel):
    id: str
    work_item_id: str
    agent_run_id: str
    phase: Literal["plan", "work", "review", "return"]
    sha256: str
    size_bytes: int
    accepted: bool | None = None


class DelegationChildDTO(BaseModel):
    delegation_id: str
    state: str


class DelegationDTO(BaseModel):
    id: str
    delegator_agent_id: str
    delegator_revision_id: str
    worker_agent_id: str
    worker_revision_id: str
    reviewer_agent_id: str
    reviewer_revision_id: str
    project_id: str
    persona_id: str | None
    parent_delegation_id: str | None
    linked_work_item_id: str | None
    created_by: str
    title: str
    work_role: str
    round: int
    content_hash: str
    cancelled_at: str | None
    created_at: str | None
    state: str
    readiness: DelegationReadiness
    phases: dict[str, dict[str, Any] | None]
    artifacts: dict[str, DelegationArtifactDTO]
    children: list[DelegationChildDTO]
    planning: dict[str, Any]
    media_input_pins: list[MediaInputPinDTO] = Field(default_factory=list)
    operations_source_ref: OperationsSourceRefDTO | None = None


class DelegationResponse(BaseModel):
    success: bool = True
    delegation: DelegationDTO


class DelegationListResponse(BaseModel):
    success: bool = True
    delegations: list[DelegationDTO]


class DelegationArtifactContent(BaseModel):
    summary: str
    body: str
    accepted: bool | None = None
    tasks: list[PlannedAssignment] | None = None


class DelegationArtifactContentDTO(DelegationArtifactDTO):
    content: DelegationArtifactContent
    evidence: dict[str, Any]
    child_returns: list[dict[str, Any]]
    context_sha256: str


class DelegationArtifactResponse(BaseModel):
    success: bool = True
    artifact: DelegationArtifactContentDTO


class OrganizationGraphResponse(BaseModel):
    success: bool = True
    nodes: list[dict[str, Any]]
    edges: list[dict[str, str]]


class OrganizationSetupEmployee(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    agent_id: UUID | None = None
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    display_name: str = Field(min_length=1, max_length=160)
    manager_key: str | None = Field(default=None, max_length=100)
    job_title: str = Field(default="", max_length=160)
    responsibility_summary: str = Field(default="", max_length=10_000)
    revision: CreateRevisionRequest | None = None
    activate: bool = False

    @model_validator(mode="after")
    def reference_required(self):
        if self.agent_id is None and self.slug is None:
            raise ValueError("agent_id or slug is required")
        return self


class OrganizationSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employees: list[OrganizationSetupEmployee] = Field(min_length=1, max_length=64)


class OrganizationSetupResponse(BaseModel):
    success: bool = True
    employees: dict[str, dict[str, Any]]
    blockers: list[dict[str, Any]]
    ready: bool


def create_agent_delegation_routers(*, get_db_manager, get_user_from_request, require_auth_dependency, config=None):
    delegation_router = APIRouter(prefix="/api/agent-delegations", tags=["Agent Delegation"], route_class=_SafeDelegationRoute)
    organization_router = APIRouter(prefix="/api/agent-organization", tags=["Agent Organization"], route_class=_SafeDelegationRoute)

    async def context(request: Request, _=Depends(require_auth_dependency)):
        user = get_user_from_request(request)
        user = await user if inspect.isawaitable(user) else user
        def field(key, default=None):
            return user.get(key, default) if isinstance(user, dict) else getattr(user, key, default)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if field("role") != "admin" or field("is_agent", False) or (field("actor_type") or field("principal_kind") or "human") != "human":
            raise HTTPException(403, "delegation_human_admin_required")
        actor_id = field("id") or field("user_id")
        manager = get_db_manager() if callable(get_db_manager) else get_db_manager
        manager = await manager if inspect.isawaitable(manager) else manager
        if manager is None:
            raise HTTPException(503, "delegation_storage_unavailable")
        return actor_id, manager

    async def invoke(awaitable):
        try:
            return await awaitable
        except DelegationError as exc:
            raise HTTPException(exc.status_code, exc.code) from None
        except AgentIdentityError as exc:
            raise HTTPException(exc.status_code, "delegation_identity_denied") from None

    @delegation_router.post("", response_model=DelegationResponse, operation_id="employee_create_delegation")
    async def create_delegation(payload: DelegationCreateRequest, ctx=Depends(context)):
        actor, manager = ctx
        order = await invoke(AgentDelegationService(manager, config=config).create(actor_user_id=actor, **payload.model_dump(mode="json", exclude_unset=True)))
        return {"success": True, "delegation": order}

    @delegation_router.get("", response_model=DelegationListResponse, operation_id="employee_list_delegations")
    async def list_delegations(agent_id: UUID | None = None, limit: int = Query(100, ge=1, le=200), ctx=Depends(context)):
        actor, manager = ctx
        rows = await invoke(AgentDelegationService(manager, config=config).list(actor_user_id=actor, agent_id=agent_id, limit=limit))
        return {"success": True, "delegations": rows}

    @delegation_router.get("/{delegation_id}", response_model=DelegationResponse, operation_id="employee_get_delegation")
    async def get_delegation(delegation_id: UUID, ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, "delegation": await invoke(AgentDelegationService(manager, config=config).get(delegation_id, actor_user_id=actor))}

    @delegation_router.post("/{delegation_id}/retry", response_model=DelegationResponse, operation_id="employee_retry_delegation")
    async def retry_delegation(delegation_id: UUID, payload: DelegationRetryRequest, ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, "delegation": await invoke(AgentDelegationService(manager, config=config).retry(
            delegation_id, actor_user_id=actor, expected_work_item_id=payload.expected_work_item_id))}

    @delegation_router.post("/{delegation_id}/cancel", response_model=DelegationResponse, operation_id="employee_cancel_delegation")
    async def cancel_delegation(delegation_id: UUID, ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, "delegation": await invoke(AgentDelegationService(manager, config=config).cancel(delegation_id, actor_user_id=actor))}

    @delegation_router.get("/{delegation_id}/artifacts/{artifact_id}", response_model=DelegationArtifactResponse, operation_id="employee_get_delegation_artifact")
    async def get_delegation_artifact(delegation_id: UUID, artifact_id: UUID, ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, "artifact": await invoke(AgentDelegationService(manager, config=config).artifact(
            delegation_id, artifact_id, actor_user_id=actor))}

    @organization_router.get("", response_model=OrganizationGraphResponse, operation_id="employee_get_organization")
    async def get_employee_organization(ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, **await invoke(AgentOrganizationService(manager, config=config).graph(actor_user_id=actor))}

    @organization_router.post("/setup", response_model=OrganizationSetupResponse, operation_id="employee_setup_organization")
    async def setup_employee_organization(payload: OrganizationSetupRequest, ctx=Depends(context)):
        actor, manager = ctx
        return {"success": True, **await invoke(AgentOrganizationService(manager, config=config).setup(
            actor_user_id=actor,
            employees=[entry.model_dump(exclude_unset=True) for entry in payload.employees],
        ))}

    return delegation_router, organization_router
