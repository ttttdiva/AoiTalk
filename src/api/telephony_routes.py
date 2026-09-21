"""Admin telephone management and separately authenticated signed ingress."""
from __future__ import annotations

import inspect
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from ..features import Features
from ..services.telephony_service import TelephonyError, TelephonyService


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default error echoes rejected input values. A
                # misplaced credential/phone must not become response content.
                return JSONResponse(status_code=422, content={"detail": "telephony_request_invalid"})
        return safe_handler


class TelephonyRouteCreate(_Command):
    display_name: str = Field(min_length=1, max_length=160)
    connection_id: UUID
    provider_route_ref: str = Field(min_length=1, max_length=80)
    agent_id: UUID
    agent_revision_id: UUID
    timezone: str = Field(default="Asia/Tokyo", max_length=64)
    business_hours_json: dict[str, Any] = Field(default_factory=dict)
    greeting_override: str = Field(default="", max_length=1200)
    transfer_policy_json: dict[str, Any] = Field(default_factory=dict)
    fallback_mode: Literal["reject"] = "reject"
    idempotency_key: str = Field(min_length=1, max_length=255)


class TelephonyRoutePatch(_Command):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    connection_id: UUID | None = None
    provider_route_ref: str | None = Field(default=None, min_length=1, max_length=80)
    agent_id: UUID | None = None
    agent_revision_id: UUID | None = None
    timezone: str | None = Field(default=None, max_length=64)
    business_hours_json: dict[str, Any] | None = None
    greeting_override: str | None = Field(default=None, max_length=1200)
    transfer_policy_json: dict[str, Any] | None = None
    fallback_mode: Literal["reject"] | None = None


class TelephonyRouteState(_Command):
    expected_version: int = Field(ge=1)
    state: Literal["draft", "active", "paused", "retired"]


def create_telephony_router(get_db_manager, get_user_from_request, require_auth_dependency, *, service=None, config=None):
    manager = get_db_manager() if callable(get_db_manager) else get_db_manager
    service = service or TelephonyService(manager, config=config)
    router = APIRouter(prefix="/api/telephony", tags=["telephony"], route_class=_PrivateValidationRoute)

    async def admin(request: Request, _=Depends(require_auth_dependency)):
        if not Features.virtual_company():
            raise HTTPException(404, "telephony_feature_disabled")
        user = get_user_from_request(request)
        if inspect.isawaitable(user):
            user = await user
        if not user:
            raise HTTPException(401, "authentication_required")
        get = user.get if isinstance(user, dict) else lambda key, default=None: getattr(user, key, default)
        if get("role") != "admin" or get("is_agent", False) or (get("actor_type", "human") not in (None, "human")):
            raise HTTPException(403, "telephony_admin_required")
        return get("id") or get("user_id")

    async def invoke(method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except TelephonyError as exc:
            raise HTTPException(exc.status_code, exc.code) from None
        except Exception:
            raise HTTPException(503, "telephony_unavailable") from None

    @router.get("/catalog")
    async def catalog(actor=Depends(admin)):
        return await invoke(service.catalog, actor_user_id=actor)

    @router.get("/routes")
    async def routes(agent_id: UUID | None = None, actor=Depends(admin)):
        return {"routes": await invoke(service.list_routes, actor_user_id=actor, agent_id=agent_id)}

    @router.post("/routes", status_code=201)
    async def create(body: TelephonyRouteCreate, actor=Depends(admin)):
        return {"route": await invoke(service.create_route, body.model_dump(), actor_user_id=actor)}

    @router.get("/routes/{route_id}")
    async def get(route_id: UUID, actor=Depends(admin)):
        return {"route": await invoke(service.get_route, route_id, actor_user_id=actor)}

    @router.patch("/routes/{route_id}")
    async def patch(route_id: UUID, body: TelephonyRoutePatch, actor=Depends(admin)):
        return {"route": await invoke(service.update_route, route_id, body.model_dump(exclude_unset=True), actor_user_id=actor)}

    @router.post("/routes/{route_id}/state")
    async def state(route_id: UUID, body: TelephonyRouteState, actor=Depends(admin)):
        return {"route": await invoke(service.update_route, route_id, body.model_dump(), actor_user_id=actor)}

    @router.get("/routes/{route_id}/readiness")
    async def readiness(route_id: UUID, actor=Depends(admin)):
        return await invoke(service.readiness, route_id, actor_user_id=actor)

    @router.get("/calls")
    async def calls(route_id: UUID | None = None, actor=Depends(admin)):
        return {"calls": await invoke(service.list_calls, actor_user_id=actor, route_id=route_id)}

    @router.get("/calls/{call_id}")
    async def call(call_id: UUID, actor=Depends(admin)):
        found = await invoke(service.list_calls, actor_user_id=actor, call_id=call_id)
        if not found:
            raise HTTPException(404, "telephony_call_not_found")
        return {"call": found[0]}

    @router.post("/webhooks/openai/{connection_id}")
    async def webhook(connection_id: UUID, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "telephony_webhook_content_type")
        chunks = bytearray()
        async for part in request.stream():
            if len(chunks) + len(part) > 65536:
                raise HTTPException(413, "telephony_webhook_too_large")
            chunks.extend(part)
        return await invoke(service.handle_webhook, connection_id, bytes(chunks), request.headers)

    return router
