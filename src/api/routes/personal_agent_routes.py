"""Signed-cookie owner Web routes for explicit personal adapter operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...services.agent_identity_service import AgentIdentityError, AgentIdentityService
from ...services.personal_agent_controller import (
    PersonalAgentController,
    PersonalAgentControllerError,
    build_personal_agent_controller,
)
from ...services.personal_agent_runtime import (
    PersonalDependencyPending,
    PersonalFinanceSummaryIntegrityError,
    PersonalFinanceSummaryOverflowError,
)
from ...services.personal_career_service import CareerError
from ...services.personal_finance_contracts import FinanceContractError
from ...services.personal_ledger_service import LedgerError
from .personal_ledger_routes import _http_error, require_interactive_owner


PersonalAgentProfileId = Literal[
    "personal-firearms-admin",
    "personal-ammunition-bookkeeper",
    "personal-finance-reviewer",
    "personal-career-planner",
]
PersonalAgentOperation = Literal[
    "deadline.state",
    "deadline.propose",
    "inventory.state",
    "inventory.propose",
    "finance.read_entry",
    "finance.summary",
    "career.context",
]


class PersonalAgentProfileOut(BaseModel):
    id: PersonalAgentProfileId
    priority: int
    kinds: list[str]
    operations: list[PersonalAgentOperation]
    registration_state: str
    autostart: Literal[False]
    execution_kind: Literal["adapter_only"]


class PersonalAgentProfilesOut(BaseModel):
    items: list[PersonalAgentProfileOut]


class PersonalAgentInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    profile_id: PersonalAgentProfileId
    operation: PersonalAgentOperation
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=16)


class PersonalAgentOwnerAgentSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=160, strict=True)
    profile_id: PersonalAgentProfileId
    idempotency_key: UUID


class PersonalAgentOwnerAgentOut(BaseModel):
    agent_id: UUID
    display_name: str
    state: Literal["active"]
    profile_id: PersonalAgentProfileId
    agent_revision_id: UUID
    revision_version: int
    legacy: dict[str, str] | None = None


class PersonalAgentOwnerAgentsOut(BaseModel):
    items: list[PersonalAgentOwnerAgentOut]


class PersonalAgentOwnerSessionOut(BaseModel):
    id: UUID
    user_id: str
    agent_id: UUID
    agent_binding_updated_at: datetime | None = None
    title: str
    character_name: str


class PersonalAgentOwnerSessionsOut(BaseModel):
    items: list[PersonalAgentOwnerSessionOut]


class PersonalAgentInvokeResponse(BaseModel):
    execution_kind: Literal["adapter_only"]
    generative_response: Literal[False]
    profile_id: PersonalAgentProfileId
    operation: PersonalAgentOperation
    agent_id: str
    agent_revision_id: str
    binding_id: str
    result: Any


def register_personal_agent_routes(
    app: FastAPI,
    server: Any,
    *,
    controller: PersonalAgentController | None = None,
    identity_service: AgentIdentityService | None = None,
) -> None:
    """Register owner-only route; callers register it during app composition."""

    personal = controller or build_personal_agent_controller(server)
    identities = identity_service or AgentIdentityService(
        getattr(server, "_db_manager", None), config=getattr(server, "config", None)
    )

    async def owner(request: Request, response: Response) -> str:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        owner_id = await require_interactive_owner(server, request)
        if request.method in {"POST", "PATCH"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "本人の同一オリジン JSON 操作が必要です")
        return owner_id

    @app.get("/api/personal-agents/profiles", response_model=PersonalAgentProfilesOut)
    async def list_profiles(owner_id: str = Depends(owner)):
        del owner_id
        return {"items": personal.list_profiles()}

    @app.get("/api/personal-agents/owner-agent", response_model=PersonalAgentOwnerAgentsOut)
    async def list_owner_agents(owner_id: str = Depends(owner)):
        try:
            return {"items": await identities.list_personal_adapter_agents(owner_user_id=owner_id)}
        except AgentIdentityError as exc:
            raise HTTPException(exc.status_code, exc.__class__.__name__) from None

    @app.get(
        "/api/personal-agents/owner-agent/sessions",
        response_model=PersonalAgentOwnerSessionsOut,
    )
    async def list_owner_agent_sessions(
        profile_id: PersonalAgentProfileId,
        owner_id: str = Depends(owner),
    ):
        try:
            return {
                "items": await personal.list_owner_sessions(
                    owner_id=owner_id,
                    profile_id=profile_id,
                )
            }
        except (AgentIdentityError, PersonalAgentControllerError) as exc:
            status_code = getattr(exc, "status_code", 403)
            detail = getattr(exc, "code", exc.__class__.__name__)
            raise HTTPException(status_code, detail) from None

    @app.post("/api/personal-agents/owner-agent", response_model=PersonalAgentOwnerAgentOut, status_code=201)
    async def create_owner_agent(body: PersonalAgentOwnerAgentSaveRequest, owner_id: str = Depends(owner)):
        try:
            return await identities.save_personal_adapter_agent(
                owner_user_id=owner_id, display_name=body.display_name,
                profile_id=body.profile_id, idempotency_key=body.idempotency_key,
            )
        except AgentIdentityError as exc:
            raise HTTPException(exc.status_code, exc.__class__.__name__) from None

    @app.patch("/api/personal-agents/owner-agent/{agent_id}", response_model=PersonalAgentOwnerAgentOut)
    async def update_owner_agent(agent_id: UUID, body: PersonalAgentOwnerAgentSaveRequest,
                                 owner_id: str = Depends(owner)):
        try:
            return await identities.save_personal_adapter_agent(
                owner_user_id=owner_id, agent_id=agent_id,
                display_name=body.display_name, profile_id=body.profile_id,
                idempotency_key=body.idempotency_key,
            )
        except AgentIdentityError as exc:
            raise HTTPException(exc.status_code, exc.__class__.__name__) from None

    @app.post("/api/personal-agents/invoke", response_model=PersonalAgentInvokeResponse)
    async def invoke(body: PersonalAgentInvokeRequest, owner_id: str = Depends(owner)):
        try:
            return await personal.invoke(
                owner_id=owner_id,
                session_id=str(body.session_id),
                profile_id=body.profile_id,
                operation=body.operation,
                arguments=body.arguments,
            )
        except PersonalAgentControllerError as exc:
            raise HTTPException(exc.status_code, exc.code) from None
        except PersonalFinanceSummaryIntegrityError:
            raise HTTPException(409, "finance_summary_integrity") from None
        except PersonalFinanceSummaryOverflowError:
            raise HTTPException(422, "finance_summary_overflow") from None
        except LedgerError as exc:
            raise _http_error(exc) from None
        except PersonalDependencyPending:
            raise HTTPException(503, "personal_dependency_pending") from None
        except (CareerError, FinanceContractError):
            raise HTTPException(409, "personal_operation_unavailable") from None


__all__ = [
    "PersonalAgentOwnerAgentOut",
    "PersonalAgentOwnerAgentSaveRequest",
    "PersonalAgentOwnerAgentsOut",
    "PersonalAgentOwnerSessionOut",
    "PersonalAgentOwnerSessionsOut",
    "PersonalAgentInvokeRequest",
    "PersonalAgentInvokeResponse",
    "PersonalAgentProfilesOut",
    "register_personal_agent_routes",
]
