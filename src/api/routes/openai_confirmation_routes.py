"""Record an owner's existing allowance facts; never enroll or call OpenAI."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ...memory.database import get_db_session
from ...services import openai_confirmation_service as confirmation


class OpenAIConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_reference: Literal["openai_api_key", "free-team:openai-complimentary"]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_record_id: str | None = None
    billing_scope_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    usage_tier: Literal["tier_1_2", "tier_3_plus"]
    eligible: StrictBool
    enrolled: StrictBool
    source_reference: str = Field(min_length=1, max_length=500)


class OpenAIConfirmationRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_record_id: str


class OpenAIConfirmationCredential(BaseModel):
    reference: str
    configured: bool
    fingerprint: str
    project_id: str
    sdk_organization_id: str
    base_url: str


class OpenAIConfirmationRecord(BaseModel):
    credential_reference: str | None = None
    record_id: str | None = None
    owner_user_id: str | None = None
    actor_user_id: str | None = None
    recorded_at: str | None = None
    source_kind: str | None = None
    source_reference: str | None = None
    eligible: bool | None = None
    enrolled: bool | None = None
    organization_id: str | None = None
    usage_tier: str | None = None
    revoked_at: str | None = None


class OpenAIConfirmationResponse(BaseModel):
    credentials: list[OpenAIConfirmationCredential]
    record: OpenAIConfirmationRecord
    stop_reason: str
    billing_scope_id: str
    usage_tier: str
    provider_attestation: Literal[False] = False


async def require_confirmation_actor(server, request: Request) -> str:
    # Internal forwarding markers are not proof of a human web session. Resolve
    # the original signed cookie again through the existing auth implementation.
    if "authorization" in request.headers or not request.headers.get("cookie"):
        raise HTTPException(401, "確認記録には本人のWebセッションが必要です")
    next_cookie = server._get_request_cookie(request, server.next_cookie_name)
    if not next_cookie:
        raise HTTPException(401, "確認記録には本人のNext Webセッションが必要です")
    scope = dict(request.scope)
    scope["headers"] = [(key, value) for key, value in request.scope.get("headers", [])
        if key.lower() not in {b"x-internal-auth", b"cookie"} and not key.lower().startswith(b"x-forwarded-")]
    scope["headers"].append((b"cookie", f"{server.next_cookie_name}={next_cookie}".encode("latin1")))
    authenticated_request = Request(scope)
    try:
        next_subject = server._get_next_user_id_from_request(authenticated_request, raise_on_db_error=True)
        if not next_subject:
            raise HTTPException(401, "確認記録には本人のNext Webセッションが必要です")
        user = await server._get_user_info_from_request(authenticated_request, raise_on_db_error=True)
    except ValueError as exc:
        raise HTTPException(401, "WebセッションのユーザーIDが不正です") from exc
    if not user:
        raise HTTPException(401, "確認記録には本人のWebセッションが必要です")
    if (getattr(server, "auth_enabled", True) is False or user.get("role") != "admin"
            or user.get("_authority_source") != "web_session" or user.get("is_agent")
            or any(user.get(key) not in {None, "", "human", "user"}
                   for key in ("actor_type", "principal_kind"))):
        raise HTTPException(403, "確認記録は本人の管理者Webセッションから操作してください")
    try:
        actor_id = str(UUID(str(user["id"])))
        if actor_id != str(UUID(str(next_subject))):
            raise HTTPException(403, "Webセッションの確認主体が一致しません")
        return actor_id
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(403, "確認記録には永続ユーザーIDが必要です") from exc


def register_openai_confirmation_routes(app: FastAPI, server) -> None:
    async def actor(request: Request) -> str:
        return await require_confirmation_actor(server, request)

    def refresh(openai: dict) -> None:
        # Only non-secret local policy/evidence fields. No runtime restart or SDK
        # rebuild, and no credential purpose/provider setting changes.
        for key in ("complimentary_verification", "billing_scope_id", "usage_tier", "data_sharing_incentive_enabled"):
            server.config.set(f"openai.{key}", openai.get(key))

    @app.get("/api/free-team/openai-confirmation", response_model=OpenAIConfirmationResponse)
    async def read_confirmation(actor_id: str = Depends(actor)):
        session = await get_db_session()
        try:
            return await confirmation.context(session, actor_id)
        except confirmation.ConfirmationError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        finally:
            await session.close()

    @app.put("/api/free-team/openai-confirmation", response_model=OpenAIConfirmationResponse)
    async def save_confirmation(body: OpenAIConfirmationRequest, actor_id: str = Depends(actor)):
        session = await get_db_session()
        try:
            async with session.begin():
                openai = await confirmation.save(session, actor_id, body.model_dump())
            refresh(openai)
            return await confirmation.context(session, actor_id)
        except confirmation.ConfirmationError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        finally:
            await session.close()

    @app.delete("/api/free-team/openai-confirmation", response_model=OpenAIConfirmationResponse)
    async def revoke_confirmation(body: OpenAIConfirmationRevokeRequest, actor_id: str = Depends(actor)):
        session = await get_db_session()
        try:
            async with session.begin():
                openai = await confirmation.revoke(session, actor_id, body.expected_record_id)
            refresh(openai)
            return await confirmation.context(session, actor_id)
        except confirmation.ConfirmationError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        finally:
            await session.close()
