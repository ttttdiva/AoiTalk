"""Default-off Aoi owner API for the external DOTS dialogue ledger."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...services.dots_dialogue_service import (
    DedicatedDotsPrincipal,
    DialoguePrincipal,
    DotsDialogueError,
    DotsDialogueService,
    VerifiedMcpPrincipal,
)


def dots_dialogue_route_enabled(config: Any, *, enterprise_profile: bool) -> bool:
    """Strict opt-in gate. Environment strings and Enterprise profiles stay disabled."""
    try:
        raw = config.get("dots_dialogue.enabled", False)
        if raw is False and isinstance(config, Mapping):
            section = config.get("dots_dialogue", {})
            raw = section.get("enabled", False) if isinstance(section, Mapping) else False
        return raw is True and enterprise_profile is False
    except Exception:
        return False


class BindingConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intended_chat_label: str = Field(min_length=1, max_length=120)
    owner_confirmed: bool


class DialogueRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    conversation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=32_768)


def create_dots_dialogue_router(*, get_db_manager: Callable[[], Any],
                                get_user_from_request: Callable[[Request], Awaitable[dict[str, Any]]],
                                require_auth_dependency: Callable[..., Any],
                                get_mcp_principal: Callable[[Request], DialoguePrincipal | None],
                                enabled: bool = False,
                                profile_allows_external: bool = False) -> APIRouter:
    """Create owner-authenticated routes; an explicit trust provider is still required for binding."""
    router = APIRouter(prefix="/api/dots/dialogue", tags=["external DOTS dialogue"])
    service = DotsDialogueService(enabled=enabled is True and profile_allows_external is True)

    async def owner_id(request: Request) -> UUID:
        try:
            info = await get_user_from_request(request)
            return UUID(str(info["id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(status_code=401, detail={"code": "not_authenticated"}) from None

    def principal(request: Request) -> DialoguePrincipal:
        value = get_mcp_principal(request)
        if not isinstance(value, (VerifiedMcpPrincipal, DedicatedDotsPrincipal)):
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"})
        try:
            _ = value.fingerprint
        except DotsDialogueError:
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"}) from None
        return value

    async def run(request: Request, callback: Callable[[Any], Awaitable[Any]]) -> Any:
        db_manager = get_db_manager()
        session = await db_manager.get_session()
        try:
            result = await callback(session)
            await session.commit()
            return result
        except DotsDialogueError as exc:
            await session.rollback()
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from None
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @router.get("/bindings", dependencies=[Depends(require_auth_dependency)])
    async def list_bindings(request: Request):
        actor = await owner_id(request)
        return {"bindings": await run(request, lambda db: service.list_bindings(db, owner_user_id=actor))}

    @router.get("/availability", include_in_schema=False, dependencies=[Depends(require_auth_dependency)])
    async def dedicated_chat_availability(
        request: Request,
        conversation_id: UUID | None = Query(default=None),
    ):
        actor = await owner_id(request)
        if service.enabled is not True:
            return {"selectable": False, "state": "feature_unavailable"}
        try:
            trusted = get_mcp_principal(request)
        except Exception:
            trusted = None
        if (
            not isinstance(trusted, DedicatedDotsPrincipal)
            or trusted.owner_user_id != actor
        ):
            return {"selectable": False, "state": "connection_unavailable"}
        return await run(request, lambda db: service.get_dedicated_chat_availability(
            db, owner_user_id=actor, principal=trusted,
            conversation_id=conversation_id,
        ))

    @router.post("/bindings", status_code=201, dependencies=[Depends(require_auth_dependency)])
    async def confirm_binding(payload: BindingConfirmation, request: Request):
        actor = await owner_id(request)
        trusted = principal(request)
        binding = await run(request, lambda db: service.confirm_binding(
            db, owner_user_id=actor, principal=trusted,
            intended_chat_label=payload.intended_chat_label,
            owner_confirmed=payload.owner_confirmed,
        ))
        return {"binding_id": str(binding.id), "intended_chat_label": binding.intended_chat_label,
                "state": binding.state, "generation": binding.generation,
                "owner_confirmed_at": binding.owner_confirmed_at.strftime("%Y-%m-%dT%H:%M:%SZ")}

    @router.delete("/bindings/{binding_id}", status_code=204, dependencies=[Depends(require_auth_dependency)])
    async def revoke_binding(binding_id: UUID, request: Request):
        actor = await owner_id(request)
        await run(request, lambda db: service.revoke_binding(db, owner_user_id=actor, binding_id=binding_id))
        return None

    @router.post("/requests", status_code=202, dependencies=[Depends(require_auth_dependency)])
    async def create_request(payload: DialogueRequestCreate, request: Request):
        actor = await owner_id(request)
        trusted = principal(request)
        receipt = await run(request, lambda db: service.enqueue_request(
            db, owner_user_id=actor, binding_id=payload.binding_id,
            conversation_id=payload.conversation_id, idempotency_key=payload.idempotency_key,
            message=payload.message, principal=trusted,
        ))
        return {"request_id": str(receipt.request_id), "conversation_id": str(receipt.conversation_id),
                "state": receipt.state, "created_at": receipt.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "user_message_id": str(receipt.user_message_id)}

    @router.get("/requests/{request_id}", dependencies=[Depends(require_auth_dependency)])
    async def read_request(request_id: UUID, request: Request):
        actor = await owner_id(request)
        return await run(request, lambda db: service.get_owner_request(
            db, owner_user_id=actor, request_id=request_id,
        ))

    @router.delete("/requests/{request_id}", status_code=204, dependencies=[Depends(require_auth_dependency)])
    async def cancel_request(request_id: UUID, request: Request):
        actor = await owner_id(request)
        await run(request, lambda db: service.cancel_request(
            db, owner_user_id=actor, request_id=request_id,
        ))
        return None

    return router
