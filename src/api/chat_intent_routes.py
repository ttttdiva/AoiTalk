"""Authenticated, non-persistent suggestions for a chat draft's intent."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from time import perf_counter
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.chat_intent_service import (
    CHAT_INTENTS,
    MAX_CHAT_INTENT_TEXT_LENGTH,
    ChatIntentInputError,
    ChatIntentService,
    validate_chat_intent_text,
)
from ..services.jev_decision_service import JevClientPool, JevError
from ..services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    PrivacyReviewDenied,
)

MAX_CHAT_INTENT_REQUEST_BYTES = 32768
CHAT_INTENT_TOTAL_TIMEOUT_SECONDS = 18.0
logger = logging.getLogger(__name__)


def _success_timing_header(timings: Mapping[str, Any]) -> str:
    # Fixed metric names and finite numeric durations only; no content,
    # identifiers, destinations, model names, or provider-controlled labels.
    return ", ".join(
        f"chat_intent_{name};dur={value:.3f}"
        for name in (
            "body",
            "auth",
            "scope",
            "inference",
            "gateway",
            "provider",
            "total",
            "connect_tcp",
            "start_tls",
            "loop_lag",
            "executor_queue",
        )
        if type(value := timings.get(name)) in {int, float}
        and math.isfinite(value)
        and value >= 0
    )


class ChatIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        ..., strict=True, min_length=1, max_length=MAX_CHAT_INTENT_TEXT_LENGTH
    )
    session_id: UUID | None = None
    project_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return validate_chat_intent_text(value)


class ChatIntentCandidate(BaseModel):
    id: str
    label: str
    score: float = Field(..., ge=0, le=1, allow_inf_nan=False)


class ChatIntentResponse(BaseModel):
    candidates: list[ChatIntentCandidate] = Field(
        ..., min_length=len(CHAT_INTENTS), max_length=len(CHAT_INTENTS)
    )
    model: str = Field(..., pattern=r"^jev-[\w.\-]{1,72}$")


class ChatIntentErrorDetail(BaseModel):
    code: str
    message: str


class ChatIntentErrorResponse(BaseModel):
    # The shared authentication guard may still use a plain detail string.
    detail: ChatIntentErrorDetail | str


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers={"Cache-Control": "no-store"},
    )


def _timeout_error(
    stage: str,
    timeout_kind: str | None = None,
    *,
    transport_phase: str | None = None,
    transport_timings_ms: Mapping[str, Any] | None = None,
    executor_probe_state: str | None = None,
) -> HTTPException:
    # Only fixed stage names are diagnostic. Never log the draft, identities,
    # scope IDs, provider payload, credentials, or the underlying exception.
    if stage not in {
        "request_body",
        "authentication",
        "scope",
        "inference",
        "provider",
    }:
        stage = "inference"
    if not isinstance(timeout_kind, str) or timeout_kind not in {
        "connect",
        "read",
        "write",
        "pool",
        "overall",
        "unknown",
    }:
        timeout_kind = None
    if timeout_kind is None:
        logger.warning("chat_intent_timeout stage=%s", stage)
    else:
        logger.warning("chat_intent_timeout stage=%s subtype=%s", stage, timeout_kind)
    error = _error(504, "chat_intent_timeout", "意図候補の取得がタイムアウトしました")
    timing_header = f'chat_intent_timeout;desc="{stage}"'
    if timeout_kind is not None:
        timing_header += f', chat_intent_timeout_kind;desc="{timeout_kind}"'
    if isinstance(transport_phase, str) and transport_phase in {
        "connect_tcp",
        "start_tls",
        "connected",
        "unobserved",
    }:
        timing_header += f', chat_intent_transport_phase;desc="{transport_phase}"'
        logger.warning("chat_intent_transport phase=%s", transport_phase)
    if isinstance(transport_timings_ms, Mapping):
        metrics = _success_timing_header(
            {
                name: transport_timings_ms.get(name)
                for name in ("connect_tcp", "start_tls", "loop_lag", "executor_queue")
            }
        )
        if metrics:
            timing_header += f", {metrics}"
            logger.warning("chat_intent_transport_timing %s", metrics)
    if isinstance(executor_probe_state, str) and executor_probe_state in {
        "completed",
        "pending",
        "unavailable",
    }:
        timing_header += f', chat_intent_executor_probe;desc="{executor_probe_state}"'
        logger.warning("chat_intent_executor_probe state=%s", executor_probe_state)
    error.headers = {**(error.headers or {}), "Server-Timing": timing_header}
    return error


class _BoundedIntentRoute(APIRoute):
    """Bound JSON buffering and avoid echoing rejected draft text in errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            started = perf_counter()
            timings: dict[str, float] = {}
            request.state.chat_intent_timings = timings

            async def read_body() -> bytes:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_CHAT_INTENT_REQUEST_BYTES:
                        raise _error(
                            413, "chat_intent_request_too_large", "下書きが長すぎます"
                        )
                    body.extend(chunk)
                return bytes(body)

            request.state.chat_intent_timeout_stage = "request_body"
            try:
                # One deadline includes body reception, FastAPI auth
                # dependencies, current-user lookup, scope reads, privacy
                # review, and inference. Do not restart it after auth: the
                # browser's 20-second deadline includes these stages too.
                async with asyncio.timeout(CHAT_INTENT_TOTAL_TIMEOUT_SECONDS):
                    try:
                        # Starlette's cached body is consumed by FastAPI's
                        # ordinary parser; the unbounded parser is never first.
                        request._body = await asyncio.wait_for(read_body(), timeout=5.0)
                    except asyncio.TimeoutError:
                        raise _error(
                            408,
                            "chat_intent_request_timeout",
                            "リクエストを受信できませんでした",
                        ) from None
                    request.state.chat_intent_timeout_stage = "authentication"
                    auth_started = perf_counter()
                    timings["body"] = (auth_started - started) * 1000
                    request.state.chat_intent_auth_started = auth_started
                    response = await handler(request)
                    response.headers["Cache-Control"] = "no-store"
                    timings["total"] = (perf_counter() - started) * 1000
                    timing_header = _success_timing_header(timings)
                    existing_timing = response.headers.get("Server-Timing")
                    response.headers["Server-Timing"] = (
                        f"{existing_timing}, {timing_header}"
                        if existing_timing
                        else timing_header
                    )
                    return response
            except asyncio.TimeoutError:
                raise _timeout_error(request.state.chat_intent_timeout_stage) from None
            except RequestValidationError:
                raise _error(
                    422,
                    "chat_intent_invalid_request",
                    "下書きと会話の指定を確認してください",
                ) from None

        return bounded_handler


def _field(record: Any, name: str) -> Any:
    return (
        record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)
    )


def _privacy_mode(value: Any) -> dict[str, str]:
    # Only the server-owned policy needed by effective_privacy_mode is copied;
    # no conversation content or unrelated project metadata reaches Jev.
    if not isinstance(value, Mapping) or value.get("privacy_mode") is None:
        return {}
    mode = str(value["privacy_mode"]).strip().lower()
    if mode not in {"direct", "protected", "local_only"}:
        raise _error(
            503, "chat_intent_scope_unavailable", "会話の保護設定を確認できません"
        )
    return {"privacy_mode": mode}


async def _load_session(session_id: UUID, user_id: str) -> Any:
    from ..memory.conversation_repository import ConversationRepository

    repository = ConversationRepository()
    if not await repository.user_has_session_write_access(str(session_id), user_id):
        raise _error(403, "chat_intent_scope_denied", "会話にアクセスできません")
    conversation = await repository.get_session_by_id(
        str(session_id), with_messages=False
    )
    if conversation is None:
        raise _error(403, "chat_intent_scope_denied", "会話にアクセスできません")
    return conversation


async def _load_project(project_id: UUID, user_id: str) -> Any:
    from sqlalchemy import select

    from ..memory.database import get_database_manager
    from ..memory.models import Project
    from ..memory.project_repository import ProjectRepository

    database = get_database_manager()
    if database is None:
        raise _error(
            503, "chat_intent_scope_unavailable", "会話の保護設定を確認できません"
        )
    session = await database.get_session()
    try:
        if not await ProjectRepository.has_permission(
            session, project_id=project_id, user_id=UUID(user_id), permission="read"
        ):
            raise _error(
                403, "chat_intent_scope_denied", "プロジェクトにアクセスできません"
            )
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project is None:
            raise _error(
                403, "chat_intent_scope_denied", "プロジェクトにアクセスできません"
            )
        # Copy the value while the row is attached to its database session.
        return {"project_metadata": _field(project, "project_metadata")}
    finally:
        await session.close()


async def _resolve_privacy_scope(
    body: ChatIntentRequest, user_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    session_context: dict[str, str] = {}
    project_id = body.project_id
    try:
        if body.session_id is not None:
            conversation = await _load_session(body.session_id, user_id)
            session_context = _privacy_mode(_field(conversation, "context"))
            bound_project = _field(conversation, "project_id")
            bound_project_id = UUID(str(bound_project)) if bound_project else None
            if project_id is not None and project_id != bound_project_id:
                raise _error(
                    403, "chat_intent_scope_denied", "会話とプロジェクトが一致しません"
                )
            project_id = bound_project_id
        project_metadata = (
            _privacy_mode(
                _field(await _load_project(project_id, user_id), "project_metadata")
            )
            if project_id is not None
            else {}
        )
        return session_context, project_metadata
    except HTTPException:
        raise
    except Exception:
        # Database/ACL errors must not silently downgrade to global policy.
        raise _error(
            503, "chat_intent_scope_unavailable", "会話の保護設定を確認できません"
        ) from None


def create_chat_intent_router(
    *,
    require_auth_dependency,
    get_current_user,
    config: Any,
    client_pool: JevClientPool | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/chat", tags=["chat"], route_class=_BoundedIntentRoute
    )
    in_flight: set[str] = set()

    @router.post(
        "/intent-candidates",
        response_model=ChatIntentResponse,
        operation_id="get_chat_intent_candidates",
        responses={
            status: {"model": ChatIntentErrorResponse}
            for status in (401, 403, 408, 413, 422, 429, 502, 503, 504)
        },
    )
    async def get_chat_intent_candidates(
        body: ChatIntentRequest,
        request: Request,
        response: Response,
        _: None = Depends(require_auth_dependency),
    ) -> ChatIntentResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            user = await get_current_user(request)
        except Exception:
            raise _error(
                503, "chat_intent_auth_unavailable", "認証情報を確認できません"
            ) from None
        user_id = str(user.get("id") or "").strip() if isinstance(user, Mapping) else ""
        auth_finished = perf_counter()
        timings = request.state.chat_intent_timings
        timings["auth"] = (
            auth_finished - request.state.chat_intent_auth_started
        ) * 1000
        if not user_id or user_id in {"default", "default_user"}:
            raise _error(401, "chat_intent_auth_required", "ログインしてください")
        # Bound parallel paid requests without retaining draft content or
        # creating background jobs. Cancellation always releases the slot.
        if user_id in in_flight or len(in_flight) >= 16:
            raise _error(
                429, "chat_intent_busy", "意図候補の取得中です。少し待ってください"
            )
        in_flight.add(user_id)
        try:
            request.state.chat_intent_timeout_stage = "scope"
            session_context, project_metadata = await _resolve_privacy_scope(
                body, user_id
            )
            scope_finished = perf_counter()
            timings["scope"] = (scope_finished - auth_finished) * 1000
            request.state.chat_intent_timeout_stage = "inference"
            gateway = OutboundPrivacyGateway(
                config,
                user_id=user_id,
                session_id=str(body.session_id) if body.session_id else None,
                session_context=session_context,
                project_metadata=project_metadata,
            )
            service = (
                ChatIntentService(gateway, client_pool=client_pool)
                if client_pool is not None
                else ChatIntentService(gateway)
            )
            result = ChatIntentResponse.model_validate(
                await service.candidates(body.text)
            )
            timings["inference"] = (perf_counter() - scope_finished) * 1000
            service_timings = getattr(service, "timings_ms", None)
            if isinstance(service_timings, Mapping):
                for name in (
                    "gateway",
                    "provider",
                    "connect_tcp",
                    "start_tls",
                    "loop_lag",
                    "executor_queue",
                ):
                    timings[name] = service_timings.get(name)
            return result
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            # Preserve safe mapping for an inner timeout raised before the
            # route-wide deadline. The deadline itself cancels this coroutine
            # and is translated only by _BoundedIntentRoute.
            raise _timeout_error("inference") from None
        except (ExternalProviderBlocked, PrivacyReviewDenied):
            raise _error(
                403,
                "chat_intent_privacy_blocked",
                "保護設定または確認結果により意図候補を取得できません",
            ) from None
        except PrivacyError:
            raise _error(
                503,
                "chat_intent_privacy_unavailable",
                "下書きの保護を確認できませんでした",
            ) from None
        except ChatIntentInputError:
            raise _error(
                422, "chat_intent_invalid_request", "下書きを確認してください"
            ) from None
        except JevError as exc:
            if exc.code == "jev_timeout":
                raise _timeout_error(
                    "provider",
                    exc.timeout_kind,
                    transport_phase=exc.transport_phase,
                    transport_timings_ms=exc.transport_timings_ms,
                    executor_probe_state=exc.executor_probe_state,
                ) from None
            if exc.code in {
                "jev_credential_missing",
                "jev_credential_invalid",
                "jev_access_denied",
            }:
                raise _error(
                    503, "chat_intent_unavailable", "意図候補は現在利用できません"
                ) from None
            if exc.code in {"jev_rate_limited", "jev_overloaded"}:
                raise _error(
                    429, "chat_intent_busy", "意図候補が混み合っています"
                ) from None
            raise _error(
                502, "chat_intent_provider_failed", "意図候補を取得できませんでした"
            ) from None
        except Exception:
            raise _error(
                503, "chat_intent_unavailable", "意図候補は現在利用できません"
            ) from None
        finally:
            in_flight.discard(user_id)

    return router
