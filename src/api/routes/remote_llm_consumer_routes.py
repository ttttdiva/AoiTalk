"""Authenticated HTTP boundary for fixed-workflow remote LLM consumers."""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...services.remote_consumer_auth import RemoteConsumerAuth, RemoteConsumerAuthUnavailable
from ...services.remote_llm_consumers import (
    ConsumerOperationNotFound,
    ConsumerUnavailable,
    RemoteLLMConsumers,
)


class ConsumerTurnRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consumer_app: Literal['73', '86']
    provider: Literal['muse', 'grok_bot']
    operation_id: str = Field(min_length=1, max_length=128)
    system: str = Field(max_length=50000)
    user: str = Field(min_length=1, max_length=50000)


class ConsumerTurnResponse(BaseModel):
    state: str
    text: str | None = None


async def _extract_principal(request: Request, get_current_user: Callable[..., Any],
                             consumer_app: str, ticket_auth: RemoteConsumerAuth | None) -> tuple[str, str]:
    tickets = request.headers.getlist('x-remote-consumer-ticket')
    if tickets:
        if len(tickets) != 1:
            raise HTTPException(status_code=401, detail={'code': 'authentication_required'})
        if ticket_auth is None:
            raise HTTPException(status_code=503, detail={'category': 'consumer_unavailable', 'code': 'consumer_auth_unavailable'})
        try:
            principal = await ticket_auth.verify(tickets[0], consumer_app)
        except RemoteConsumerAuthUnavailable:
            raise HTTPException(status_code=503, detail={'category': 'consumer_unavailable', 'code': 'consumer_auth_unavailable'}) from None
    else:
        principal = get_current_user(request)
    if inspect.isawaitable(principal):
        principal = await principal
    if not principal:
        raise HTTPException(status_code=401, detail={'code': 'authentication_required'})
    if isinstance(principal, Mapping):
        identity = principal.get('id') or principal.get('user_id')
        role = principal.get('role')
    else:
        identity = getattr(principal, 'id', None) or getattr(principal, 'user_id', None)
        role = getattr(principal, 'role', None)
    try:
        owner_id = str(UUID(str(identity)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail={'code': 'principal_invalid'}) from None
    if not isinstance(role, str) or not role.strip():
        raise HTTPException(status_code=403, detail={'code': 'principal_invalid'})
    return owner_id, role


def _map_consumer_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ConsumerOperationNotFound):
        return HTTPException(status_code=404, detail={'code': exc.code})
    if isinstance(exc, ConsumerUnavailable):
        from ..http_errors import CONSUMER_UNAVAILABLE_CATEGORY
        return HTTPException(
            status_code=503,
            detail={'category': CONSUMER_UNAVAILABLE_CATEGORY, 'code': exc.code},
        )
    # Never expose upstream messages, settings, transport details or secrets.
    from ...services.cli_development_sessions import (
        CLIDevelopmentSessionError,
        CLIDevelopmentSessionForbidden,
        CLIDevelopmentSessionNotFound,
    )
    from ...services.grok_bot_chat_service import GrokBotChatError, GrokBotChatUnavailable
    if isinstance(exc, CLIDevelopmentSessionNotFound):
        return HTTPException(status_code=404, detail={'code': 'consumer_operation_not_found'})
    if isinstance(exc, CLIDevelopmentSessionForbidden):
        code = exc.code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', exc.code) else 'forbidden'
        return HTTPException(status_code=403, detail={'code': code})
    if isinstance(exc, CLIDevelopmentSessionError):
        code = exc.code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', exc.code) else 'provider_error'
        return HTTPException(status_code=exc.status_code, detail={'code': code})
    if exc.__class__.__name__ == 'GrokBotChatForbidden' or getattr(exc, 'code', None) == 'CHAT_OWNER_MISMATCH':
        code = exc.code if hasattr(exc, 'code') and re.fullmatch(r'[A-Za-z0-9_]{1,80}', str(exc.code)) else 'forbidden'
        return HTTPException(status_code=403, detail={'code': code})
    if isinstance(exc, (GrokBotChatError, GrokBotChatUnavailable)):
        code = exc.code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', exc.code) else 'provider_error'
        status = 409 if isinstance(exc, GrokBotChatError) else 503
        return HTTPException(status_code=status, detail={'code': code})
    return HTTPException(status_code=503, detail={'code': 'provider_error'})


def create_remote_llm_consumer_router(*, get_current_user: Callable[..., Any],
                                      service: RemoteLLMConsumers, ticket_auth: RemoteConsumerAuth | None = None) -> APIRouter:
    router = APIRouter(prefix='/api/remote-llm-consumers', tags=['remote-llm-consumers'])

    @router.post('/turns', response_model=ConsumerTurnResponse)
    async def turn(body: ConsumerTurnRequest, request: Request) -> dict[str, Any]:
        owner_id, role = await _extract_principal(request, get_current_user, body.consumer_app, ticket_auth)
        try:
            return await service.turn(owner_id=owner_id, role=role, **body.model_dump())
        except Exception as exc:
            raise _map_consumer_exception(exc) from None

    @router.get('/turns/{consumer_app}/{provider}/{operation_id}', response_model=ConsumerTurnResponse)
    async def get_turn(
        request: Request,
        consumer_app: Literal['73', '86'],
        provider: Literal['muse', 'grok_bot'],
        operation_id: str = Path(min_length=1, max_length=128),
        after: str | None = Query(default=None),
    ) -> dict[str, Any]:
        owner_id, role = await _extract_principal(request, get_current_user, consumer_app, ticket_auth)
        try:
            return await service.get_turn(
                owner_id=owner_id,
                role=role,
                consumer_app=consumer_app,
                provider=provider,
                operation_id=operation_id,
                after=after,
            )
        except Exception as exc:
            raise _map_consumer_exception(exc) from None

    return router
