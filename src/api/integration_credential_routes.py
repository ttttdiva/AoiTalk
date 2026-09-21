"""Write-only credential commands, separate from Operations connection CRUD."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from ..security.integration_credential_crypto import IntegrationCredentialCryptoError, IntegrationCredentialKeyUnavailable
from ..services.integration_credential_vault_service import (
    IntegrationCredentialError, IntegrationCredentialVaultService, require_integration_features,
)
from .operations_routes import _actor, _authenticated_human_actor, _with_session


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class IntegrationCredentialPayload(_Command):
    api_key: SecretStr = Field(min_length=1, max_length=8192, repr=False, json_schema_extra={"writeOnly": True})
    webhook_secret: SecretStr | None = Field(default=None, min_length=1, max_length=8192,
                                             repr=False, json_schema_extra={"writeOnly": True})


class IntegrationCredentialUploadRequest(_Command):
    credential_kind: Literal["api_key"]
    payload: IntegrationCredentialPayload = Field(repr=False, json_schema_extra={"writeOnly": True})
    expected_revision: int = Field(ge=0)


class IntegrationCredentialRevisionRequest(_Command):
    expected_revision: int = Field(ge=1)


class IntegrationCredentialReadinessResponse(BaseModel):
    ready: bool
    status: str
    reason_code: str
    credential_id: str | None
    revision: int | None
    state_hash: str | None
    connection_version: int
    capabilities: dict[str, Literal["available", "unknown", "unsupported"]]


class IntegrationCredentialResponse(BaseModel):
    id: str
    connection_id: str
    owner_user_id: str
    project_id: str | None
    credential_kind: str
    revision: int
    status: str
    state_hash: str
    capabilities: dict[str, Literal["available", "unknown", "unsupported"]]
    verified_at: str | None
    created_at: str | None
    updated_at: str | None


class IntegrationCredentialEnvelope(BaseModel):
    credential: IntegrationCredentialResponse | None
    readiness: IntegrationCredentialReadinessResponse


class IntegrationCredentialAuditResponse(BaseModel):
    id: str
    credential_id: str
    connection_id: str
    revision: int
    event_type: str
    actor_id: str | None
    actor_type: str
    service_actor_key: str | None
    state_hash: str
    provider_code: str | None
    created_at: str | None


class IntegrationCredentialAuditEnvelope(BaseModel):
    items: list[IntegrationCredentialAuditResponse]


class _RedactedCredentialRoute(APIRoute):
    """Local error handling also covers malformed path/query and JSON bodies.

Pydantic hide_input_in_errors protects str(exc), but FastAPI's normal error
response includes exc.errors()['input']. Do not pass those errors onward.
"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def redacted(request: Request):
            try:
                require_integration_features()
                response = await handler(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError:
                raise HTTPException(422, "integration_request_invalid") from None
            except IntegrationCredentialError as exc:
                raise HTTPException(exc.status_code, exc.code) from None
            except IntegrationCredentialKeyUnavailable:
                raise HTTPException(503, "integration_key_unavailable") from None
            except IntegrationCredentialCryptoError:
                raise HTTPException(422, "integration_credential_invalid") from None
            except SQLAlchemyError:
                # DB driver errors can include parameter values/ciphertext.
                raise HTTPException(409, "integration_storage_conflict") from None
        return redacted


async def _body(request: Request, model: type[_Command]):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 32 * 1024:
            raise HTTPException(413, "integration_request_too_large")
        chunks.append(chunk)
    try:
        return model.model_validate(json.loads(b"".join(chunks)))
    except Exception:
        raise HTTPException(422, "integration_request_invalid") from None


def _body_schema(model):
    # Inline definitions so the manually parsed boundary remains type-safe in
    # OpenAPI/generated clients without leaking raw values on validation.
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return {**expand(definitions[value["$ref"].rsplit("/", 1)[-1]]),
                        **{key: expand(item) for key, item in value.items() if key != "$ref"}}
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return {"requestBody": {"required": True, "content": {"application/json": {"schema": expand(schema)}}}}


def create_integration_credential_router(get_db_manager, get_user_from_request, require_auth_dependency,
                                         *, vault: IntegrationCredentialVaultService | None = None) -> APIRouter:
    vault = vault or IntegrationCredentialVaultService()
    router = APIRouter(prefix="/api/integrations/connections", tags=["integration-credentials"],
                       route_class=_RedactedCredentialRoute,
                       dependencies=[Depends(require_auth_dependency)])

    async def invoke(request, method, connection_id, **kwargs):
        actor = _authenticated_human_actor(await _actor(get_user_from_request, request))

        async def execute(session):
            try:
                return await method(session, actor, connection_id, **kwargs)
            except Exception:
                await session.rollback()
                raise
        return await _with_session(get_db_manager, execute)

    @router.get("/{connection_id}/credential", response_model=IntegrationCredentialEnvelope,
                operation_id="integration_get_credential")
    async def get_credential(request: Request, connection_id: UUID):
        return await invoke(request, vault.get_credential, connection_id)

    async def upload(request, connection_id, *, replace):
        command = await _body(request, IntegrationCredentialUploadRequest)
        if (replace and command.expected_revision < 1) or (not replace and command.expected_revision != 0):
            raise HTTPException(422, "integration_request_invalid")
        payload = {"api_key": command.payload.api_key.get_secret_value()}
        if command.payload.webhook_secret is not None:
            payload["webhook_secret"] = command.payload.webhook_secret.get_secret_value()
        return await invoke(request, vault.upload_credential, connection_id,
                            credential_kind=command.credential_kind, payload=payload,
                            expected_revision=command.expected_revision)

    @router.post("/{connection_id}/credential", response_model=IntegrationCredentialEnvelope,
                 operation_id="integration_upload_credential", openapi_extra=_body_schema(IntegrationCredentialUploadRequest))
    async def upload_credential(request: Request, connection_id: UUID):
        return await upload(request, connection_id, replace=False)

    @router.put("/{connection_id}/credential", response_model=IntegrationCredentialEnvelope,
                operation_id="integration_replace_credential", openapi_extra=_body_schema(IntegrationCredentialUploadRequest))
    async def replace_credential(request: Request, connection_id: UUID):
        return await upload(request, connection_id, replace=True)

    @router.post("/{connection_id}/credential/verify", response_model=IntegrationCredentialEnvelope,
                 operation_id="integration_verify_credential", openapi_extra=_body_schema(IntegrationCredentialRevisionRequest))
    async def verify_credential(request: Request, connection_id: UUID):
        command = await _body(request, IntegrationCredentialRevisionRequest)
        return await invoke(request, vault.verify_credential, connection_id, expected_revision=command.expected_revision)

    @router.post("/{connection_id}/credential/disable", response_model=IntegrationCredentialEnvelope,
                 operation_id="integration_disable_credential", openapi_extra=_body_schema(IntegrationCredentialRevisionRequest))
    async def disable_credential(request: Request, connection_id: UUID):
        command = await _body(request, IntegrationCredentialRevisionRequest)
        return await invoke(request, vault.disable_credential, connection_id, expected_revision=command.expected_revision)

    @router.get("/{connection_id}/credential/audit", response_model=IntegrationCredentialAuditEnvelope,
                operation_id="integration_list_credential_audit")
    async def audit(request: Request, connection_id: UUID, limit: Annotated[int, Query(ge=1, le=200)] = 100):
        return await invoke(request, vault.list_audit_events, connection_id, limit=limit)

    return router
