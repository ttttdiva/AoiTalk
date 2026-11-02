"""Default-off HTTP bridge for the external DOTS dialogue ledger.

The bridge never reads OAuth claims, owner id, display name, or session id
from request data. Its provider may return a verifier-built OAuth principal
or the exact dedicated principal after matching the separate local capability.
With no complete provider/configuration, bridge operations fail closed.

Owner identity remains in the existing cookie-authenticated owner routes. The
plugin bridge accepts no owner identifier; the ledger resolves owner ACLs from
the owner-confirmed binding associated with the server-derived principal.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections import OrderedDict
import hashlib
import ipaddress
import inspect
import json
import logging
import math
import re
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..memory.models.dots_dialogue import DotsDialogueBinding
from .dots_dialogue_service import (
    DOTS_DIALOGUE_EVENT,
    DedicatedDotsPrincipal,
    DialoguePrincipal,
    DotsDialogueError,
    DotsDialogueService,
    VerifiedMcpPrincipal,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubscribeDelivery(_StrictModel):
    mode: str = Field(min_length=1, max_length=20)
    url: str = Field(min_length=1, max_length=2048)
    secret: str = Field(min_length=1, max_length=128, repr=False)


class SubscriptionRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(max_length=16)
    delivery: SubscribeDelivery
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)
    cursor: str | None = Field(default=None, max_length=128)


class SubscriptionAuthorizationRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(max_length=16)


class UnsubscribeDelivery(_StrictModel):
    mode: str = Field(min_length=1, max_length=20)
    url: str = Field(min_length=1, max_length=2048)


class UnsubscribeRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(max_length=16)
    delivery: UnsubscribeDelivery


class ReadRequest(_StrictModel):
    delivery_token: str = Field(min_length=32, max_length=256, repr=False)


class ReplyRequest(ReadRequest):
    operation_id: str = Field(min_length=1, max_length=128)
    reply_text: str = Field(min_length=1, max_length=32_768, repr=False)


_DEFAULT_CALLBACK_VERIFICATION_TTL_SECONDS = 300.0
_CALLBACK_VERIFICATION_CACHE_LIMIT = 256
_CALLBACK_DIAGNOSTIC_MAX_ATTEMPTS = 16
_CALLBACK_PHASES = frozenset({"validation", "dns", "tcp", "tls", "http", "deadline"})
_CALLBACK_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_CALLBACK_SAFE_FAILURE_CODES = frozenset({
    "callback_timeout", "callback_address_limit", "callback_dns_failed",
    "callback_address_not_public", "invalid_callback_url", "invalid_signing_secret",
    "callback_challenge_rejected", "callback_challenge_invalid",
    "callback_response_too_large", "callback_delivery_failed",
})
logger = logging.getLogger(__name__)


def _safe_callback_diagnostics(value: Any) -> dict[str, Any]:
    """Read bounded transport facts without retaining a callback URL or error text."""
    origin = None
    raw_origin = getattr(value, "origin", None)
    if isinstance(raw_origin, str) and len(raw_origin) <= 300:
        try:
            parsed = urlsplit(raw_origin)
            if parsed.scheme.lower() == "https" and parsed.hostname and parsed.username is None and parsed.password is None:
                raw_host = parsed.hostname
                try:
                    host = ipaddress.ip_address(raw_host).compressed.lower()
                    if ":" in host:
                        host = f"[{host}]"
                except ValueError:
                    host = raw_host.encode("idna").decode("ascii").lower().rstrip(".")
                    if not host or len(host) > 253 or any(
                        not _CALLBACK_DNS_LABEL.fullmatch(label) for label in host.split(".")
                    ):
                        host = ""
                port = parsed.port or 443
                if host and 1 <= port <= 65535:
                    origin = f"https://{host}:{port}"
        except (UnicodeError, TypeError, ValueError):
            origin = None
    families_value = getattr(value, "address_families", ())
    families = tuple(
        family for family in ("ipv4", "ipv6")
        if isinstance(families_value, (tuple, list, set, frozenset)) and family in families_value
    )
    count = getattr(value, "address_count", 0)
    if not isinstance(count, int) or isinstance(count, bool):
        count = 0
    phase = getattr(value, "phase", None)
    return {
        "phase": phase if isinstance(phase, str) and phase in _CALLBACK_PHASES else "none",
        "origin": origin or "none",
        "address_families": ",".join(families) or "none",
        "address_count": min(max(count, 0), _CALLBACK_DIAGNOSTIC_MAX_ATTEMPTS + 1),
        "public_addresses_validated": (
            getattr(value, "public_addresses_validated", None)
            if isinstance(getattr(value, "public_addresses_validated", None), bool) else "unknown"
        ),
    }


def _callback_verification_key(*, callback_url: str, secret: str,
                               principal: DialoguePrincipal, subscription_id: str) -> tuple[str, ...]:
    """Build a cache key without retaining callback credentials or addresses."""
    return tuple(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in (
        callback_url,
        secret,
        principal.fingerprint,
        subscription_id,
    ))


def create_dots_dialogue_http_bridge_router(
        *, get_db_manager: Callable[[], Any],
        principal_provider: Callable[[Request], DialoguePrincipal | None] | None = None,
        callback_sender: Any | None = None,
        enabled: bool = False, profile_allows_external: bool = False,
        callback_verification_ttl_seconds: float = _DEFAULT_CALLBACK_VERIFICATION_TTL_SECONDS) -> APIRouter:
    """Build the plugin-facing bridge router without enabling or mounting it.

    The injected principal provider is the security boundary. It must consume
    an already validated, audience-bound OAuth context; it must not infer
    identity from headers or request JSON. This factory deliberately has no
    default provider because the current application has no such verifier.
    """
    if (isinstance(callback_verification_ttl_seconds, bool)
            or not isinstance(callback_verification_ttl_seconds, (int, float))
            or not math.isfinite(float(callback_verification_ttl_seconds))
            or callback_verification_ttl_seconds <= 0):
        raise ValueError("callback_verification_ttl_seconds must be a finite positive number")

    router = APIRouter(prefix="/api/dots/dialogue/bridge", tags=["external DOTS dialogue bridge"])
    service = DotsDialogueService(enabled=enabled is True and profile_allows_external is True)
    callback_verification_cache: OrderedDict[tuple[str, ...], float] = OrderedDict()
    callback_verification_lock = asyncio.Lock()

    def require_enabled() -> None:
        if enabled is not True or profile_allows_external is not True:
            raise HTTPException(status_code=503, detail={"code": "dots_dialogue_disabled"})

    def require_principal(request: Request) -> DialoguePrincipal:
        require_enabled()
        if principal_provider is None:
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"})
        try:
            principal = principal_provider(request)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"}) from None
        if not isinstance(principal, (VerifiedMcpPrincipal, DedicatedDotsPrincipal)):
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"})
        try:
            _ = principal.fingerprint
        except (DotsDialogueError, ValueError, TypeError):
            raise HTTPException(status_code=503, detail={"code": "trusted_principal_unavailable"}) from None
        return principal

    async def run(callback: Callable[[Any], Any]) -> Any:
        require_enabled()
        db_manager = get_db_manager()
        session = db_manager.get_session()
        if inspect.isawaitable(session):
            session = await session
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

    async def _authorize_subscription_binding(db: Any, principal: DialoguePrincipal,
                                              name: str, arguments: dict[str, Any]) -> dict[str, bool]:
        # Run the service's feature/principal checks before callback verification.
        await service.list_events(db, principal=principal)
        required_arguments = set() if isinstance(principal, DedicatedDotsPrincipal) else {"binding_id"}
        if name != DOTS_DIALOGUE_EVENT or set(arguments) != required_arguments:
            raise DotsDialogueError("invalid_subscription", 422)
        statement = select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == principal.fingerprint,
            DotsDialogueBinding.dedicated_conversation_id == (
                principal.conversation_id if isinstance(principal, DedicatedDotsPrincipal) else None
            ),
            DotsDialogueBinding.state == "active",
            DotsDialogueBinding.owner_confirmed_at.is_not(None),
        )
        if not isinstance(principal, DedicatedDotsPrincipal):
            try:
                binding_id = UUID(str(arguments["binding_id"]))
            except (TypeError, ValueError, AttributeError):
                raise DotsDialogueError("invalid_binding_id", 422) from None
            statement = statement.where(DotsDialogueBinding.id == binding_id)
        binding = (await db.execute(statement)).scalar_one_or_none()
        if binding is None:
            raise DotsDialogueError("binding_unavailable", 404)
        return {"authorized": True}

    async def _verify_callback_once(*, principal: DialoguePrincipal, callback_url: str,
                                    secret: str, subscription_id: str) -> Any | None:
        cache_key = _callback_verification_key(
            callback_url=callback_url,
            secret=secret,
            principal=principal,
            subscription_id=subscription_id,
        )
        async with callback_verification_lock:
            now = time.monotonic()
            expired = [key for key, expires_at in callback_verification_cache.items()
                       if expires_at <= now]
            for key in expired:
                callback_verification_cache.pop(key, None)
            expires_at = callback_verification_cache.get(cache_key)
            if expires_at is not None and expires_at > now:
                callback_verification_cache.move_to_end(cache_key)
                return None
            if expires_at is not None:
                callback_verification_cache.pop(cache_key, None)

            from plugins.AoiTalk_Plugin.aoitalk_plugin.dots_events_transport import verify_callback

            receipt = await verify_callback(
                callback_sender, url=callback_url, secret=secret,
                subscription_id=subscription_id,
            )
            callback_verification_cache[cache_key] = time.monotonic() + float(
                callback_verification_ttl_seconds)
            callback_verification_cache.move_to_end(cache_key)
            while len(callback_verification_cache) > _CALLBACK_VERIFICATION_CACHE_LIMIT:
                callback_verification_cache.popitem(last=False)
            return receipt

    @router.get("/events")
    async def list_events(request: Request):
        trusted = require_principal(request)
        events = await run(lambda db: service.list_events(db, principal=trusted))
        return {"events": events}

    @router.post("/subscriptions/authorize")
    async def authorize_subscription(payload: SubscriptionAuthorizationRequest, request: Request):
        trusted = require_principal(request)
        return await run(lambda db: _authorize_subscription_binding(
            db, trusted, payload.name, payload.arguments,
        ))

    @router.post("/subscriptions")
    async def subscribe(payload: SubscriptionRequest, request: Request):
        trusted = require_principal(request)
        if payload.delivery.mode != "webhook":
            raise HTTPException(status_code=422, detail={"code": "invalid_subscription"})
        if callback_sender is None:
            raise HTTPException(status_code=503, detail={"code": "callback_verification_unavailable"})

        # A direct HTTP caller must not bypass the Events extension's callback
        # challenge. Recheck the binding before network I/O, then let the
        # service recheck it transactionally after the challenge completes.
        await run(lambda db: _authorize_subscription_binding(db, trusted, payload.name, payload.arguments))
        canonical = json.dumps([trusted.fingerprint, payload.delivery.url, payload.name, payload.arguments],
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        subscription_id = "sub_" + hashlib.sha256(canonical).hexdigest()[:64]
        verification_started = time.monotonic()
        try:
            verification = await _verify_callback_once(
                principal=trusted, callback_url=payload.delivery.url,
                secret=payload.delivery.secret, subscription_id=subscription_id,
            )
        except Exception as exc:
            safe_code = getattr(exc, "code", None)
            if safe_code not in _CALLBACK_SAFE_FAILURE_CODES:
                safe_code = "callback_challenge_failed"
            status_code = getattr(exc, "status_code", None)
            if not isinstance(status_code, int) or isinstance(status_code, bool) or not 100 <= status_code <= 599:
                status_code = None
            attempt_count = getattr(exc, "attempt_count", 0)
            if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
                attempt_count = 0
            attempt_count = min(max(attempt_count, 0), _CALLBACK_DIAGNOSTIC_MAX_ATTEMPTS)
            transport_elapsed_ms = getattr(exc, "elapsed_ms", None)
            if not isinstance(transport_elapsed_ms, int) or isinstance(transport_elapsed_ms, bool):
                transport_elapsed_ms = None
            elapsed_ms = max(0, int((time.monotonic() - verification_started) * 1000))
            transport_diagnostics = _safe_callback_diagnostics(exc)
            logger.warning(
                "DOTS callback verification stage=challenge category=%s status_code=%s "
                "elapsed_ms=%d transport_elapsed_ms=%s attempts=%d phase=%s origin=%s "
                "address_families=%s address_count=%d public_addresses_validated=%s",
                safe_code,
                status_code if status_code is not None else "none",
                elapsed_ms,
                transport_elapsed_ms if transport_elapsed_ms is not None else "none",
                attempt_count,
                transport_diagnostics["phase"],
                transport_diagnostics["origin"],
                transport_diagnostics["address_families"],
                transport_diagnostics["address_count"],
                transport_diagnostics["public_addresses_validated"],
            )
            raise HTTPException(status_code=502, detail={"code": safe_code}) from None
        else:
            cached = verification is None
            attempt_count = 0 if cached else getattr(verification, "attempt_count", 0)
            if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
                attempt_count = 0
            attempt_count = min(max(attempt_count, 0), _CALLBACK_DIAGNOSTIC_MAX_ATTEMPTS)
            status_code = None if cached else getattr(verification, "status_code", None)
            if not isinstance(status_code, int) or isinstance(status_code, bool) or not 100 <= status_code <= 599:
                status_code = None
            transport_elapsed_ms = None if cached else getattr(verification, "elapsed_ms", None)
            if (transport_elapsed_ms is not None and
                    (not isinstance(transport_elapsed_ms, int) or isinstance(transport_elapsed_ms, bool))):
                transport_elapsed_ms = None
            elapsed_ms = max(0, int((time.monotonic() - verification_started) * 1000))
            transport_diagnostics = _safe_callback_diagnostics(verification) if not cached else {
                "phase": "none", "origin": "none", "address_families": "none",
                "address_count": 0, "public_addresses_validated": "unknown",
            }
            logger.info(
                "DOTS callback verification stage=challenge category=verified status_code=%s "
                "elapsed_ms=%d transport_elapsed_ms=%s attempts=%d cached=%s phase=%s origin=%s "
                "address_families=%s address_count=%d public_addresses_validated=%s",
                status_code if status_code is not None else "none",
                elapsed_ms,
                transport_elapsed_ms if transport_elapsed_ms is not None else "none",
                attempt_count,
                cached,
                transport_diagnostics["phase"],
                transport_diagnostics["origin"],
                transport_diagnostics["address_families"],
                transport_diagnostics["address_count"],
                transport_diagnostics["public_addresses_validated"],
            )
        return await run(lambda db: service.subscribe(
            db, principal=trusted, name=payload.name, arguments=payload.arguments,
            callback_url=payload.delivery.url, signing_secret=payload.delivery.secret,
            ttl_ms=payload.ttl_ms, cursor=payload.cursor,
        ))

    @router.post("/subscriptions/unsubscribe")
    async def unsubscribe(payload: UnsubscribeRequest, request: Request):
        trusted = require_principal(request)
        if payload.delivery.mode != "webhook":
            raise HTTPException(status_code=422, detail={"code": "invalid_subscription"})
        return await run(lambda db: service.unsubscribe(
            db, principal=trusted, name=payload.name, arguments=payload.arguments,
            callback_url=payload.delivery.url,
        ))

    @router.post("/requests/{request_id}/read")
    async def read_request(request_id: UUID, payload: ReadRequest, request: Request):
        trusted = require_principal(request)
        result = await run(lambda db: service.read_for_principal(
            db, principal=trusted, request_id=request_id, delivery_token=payload.delivery_token,
        ))
        return {
            "request_id": str(result.request_id),
            "owner_user_id": str(result.owner_user_id),
            "binding_id": str(result.binding_id),
            "conversation_id": str(result.conversation_id),
            "target_chat_id": str(result.conversation_id),
            "generation": result.generation,
            "original_text": result.original_text,
            "message": result.message,
            "state": result.state,
            "expires_at": result.expires_at.isoformat(timespec="seconds") + "Z",
        }

    @router.post("/requests/{request_id}/reply")
    async def reply_request(request_id: UUID, payload: ReplyRequest, request: Request):
        trusted = require_principal(request)
        return await run(lambda db: service.reply_for_principal(
            db, principal=trusted, request_id=request_id,
            delivery_token=payload.delivery_token, operation_id=payload.operation_id,
            text_value=payload.reply_text,
        ))

    return router


__all__ = ["create_dots_dialogue_http_bridge_router"]
