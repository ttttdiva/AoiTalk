"""Default-off MCP Events adapter for the owner's confirmed external DOTS route.

The canonical catalog server does not install it. The separately configured
dedicated stdio/Tunnel composition uses a server-owned fixed connection
principal and a different local API capability; it does not claim OAuth or
construct a principal from MCP request data.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding, ToolBinding
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams, RequestParams, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from typing_extensions import Literal

from .dots_events_transport import CallbackSender, CallbackTransportError, verify_callback
from .dots_dialogue_dedicated import (
    DedicatedDotsConnectionConfig,
    DedicatedDotsPrincipal,
)


EVENT_NAME = "aoitalk.owner_inbox.v1.created"
MCP_EVENTS_PROTOCOL_VERSION = "2026-07-28"
CALLBACK_VERIFICATION_TTL_SECONDS = 300
MAX_VERIFICATION_CACHE_ITEMS = 512


class VerifiedMcpPrincipal:
    """A stable OAuth subject identity; bearer token material is never retained."""

    __slots__ = ("client_id", "issuer", "subject")

    def __init__(self, client_id: str, issuer: str, subject: str):
        values = (client_id, issuer, subject)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in values):
            raise ValueError("verified principal is incomplete")
        self.client_id, self.issuer, self.subject = values

    @property
    def fingerprint(self) -> str:
        raw = json.dumps((self.client_id, self.issuer, self.subject),
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


EventsPrincipal = VerifiedMcpPrincipal | DedicatedDotsPrincipal


def verified_principal_from_sdk_context(_context: Any = None) -> VerifiedMcpPrincipal | None:
    """Read only the SDK's authenticated OAuth context. Stdio has no access token."""
    token = get_access_token()
    if token is None:
        return None
    try:
        client_id, issuer, subject = principal_components(token)
        if issuer is None or subject is None:
            return None
        return VerifiedMcpPrincipal(client_id, issuer, subject)
    except (TypeError, ValueError, AttributeError):
        return None


class TrustedPrincipalResolver:
    """Explicit host-owned wrapper for a verified principal resolver.

    Production composition accepts only the SDK default resolver directly.
    This wrapper is a typed synthetic-test injection point; ordinary callables
    remain confined to ``synthetic_test_context=True``.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver: Callable[[Any], VerifiedMcpPrincipal | None]):
        if not callable(resolver):
            raise TypeError("trusted principal resolver must be callable")
        self._resolver = resolver

    def __call__(self, context: Any) -> VerifiedMcpPrincipal | None:
        return self._resolver(context)


class DedicatedPrincipalResolver:
    """Per-request resolver for the one code-configured stdio connection."""

    __slots__ = ("_config",)

    def __init__(self, connection_config: DedicatedDotsConnectionConfig):
        if not isinstance(connection_config, DedicatedDotsConnectionConfig):
            raise TypeError("dedicated connection config is required")
        self._config = connection_config

    @property
    def connection_config(self) -> DedicatedDotsConnectionConfig:
        return self._config

    def __call__(self, _context: Any) -> DedicatedDotsPrincipal:
        # MCP JSON, headers and session ids are intentionally not read here.
        return self._config.principal


def is_trusted_principal_resolver(resolver: Any) -> bool:
    """Return whether the resolver is the SDK-authenticated production path."""
    return (resolver is verified_principal_from_sdk_context
            or isinstance(resolver, DedicatedPrincipalResolver))


class DialogueEventsBackend(Protocol):
    async def list_events(self, principal: EventsPrincipal) -> list[dict[str, Any]]: ...
    async def authorize_subscription(self, principal: EventsPrincipal, name: str,
                                     arguments: dict[str, Any]) -> None: ...
    async def subscribe(self, principal: EventsPrincipal, name: str, arguments: dict[str, Any],
                        delivery: dict[str, str], ttl_ms: int | None, cursor: str | None) -> dict[str, Any]: ...
    async def unsubscribe(self, principal: EventsPrincipal, name: str, arguments: dict[str, Any],
                          delivery: dict[str, str]) -> dict[str, Any]: ...
    async def read_request(self, principal: EventsPrincipal, request_id: str,
                           delivery_token: str) -> dict[str, Any]: ...
    async def reply_request(self, principal: EventsPrincipal, request_id: str,
                            delivery_token: str, operation_id: str, reply_text: str) -> dict[str, Any]: ...


class EventDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["webhook"]
    url: str = Field(min_length=1, max_length=2048)
    secret: str = Field(min_length=1, max_length=128, repr=False)


class StrictEventsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    meta: dict[str, Any] | None = Field(default=None, alias="_meta", exclude=True)


class EventsListParams(StrictEventsParams):

    cursor: str | None = None


class EventsSubscribeParams(StrictEventsParams):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    delivery: EventDelivery
    cursor: str | None = None
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)



class EventUnsubscribeDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["webhook"]
    url: str = Field(min_length=1, max_length=2048)


class EventsUnsubscribeParams(StrictEventsParams):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    delivery: EventUnsubscribeDelivery



class ReadRequestToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    request_id: str = Field(min_length=36, max_length=36)
    delivery_token: str = Field(min_length=32, max_length=256)


class ReplyRequestToolParams(ReadRequestToolParams):
    operation_id: str = Field(min_length=1, max_length=128)
    reply_text: str = Field(min_length=1, max_length=32_768)


class CallbackVerificationCache:
    """Short-lived callback proof cache keyed by authenticated principal and URL digest."""

    def __init__(self, *, ttl: float = CALLBACK_VERIFICATION_TTL_SECONDS, now: Callable[[], float] = time.monotonic):
        self._ttl = min(max(float(ttl), 1.0), CALLBACK_VERIFICATION_TTL_SECONDS)
        self._now = now
        self._expires: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    async def verify(self, sender: CallbackSender, principal: EventsPrincipal,
                     callback_url: str, secret: str, subscription_id: str) -> None:
        url_hash = hashlib.sha256(callback_url.encode("utf-8")).hexdigest()
        key = (principal.fingerprint, url_hash)
        now = self._now()
        async with self._lock:
            self._expires = {identity: expiry for identity, expiry in self._expires.items() if expiry > now}
            if self._expires.get(key, 0) > now:
                return
            # Keep one bounded handshake in flight per cache so concurrent
            # refresh calls cannot fan out verification POSTs to one URL.
            await verify_callback(sender, url=callback_url, secret=secret, subscription_id=subscription_id)
            if len(self._expires) >= MAX_VERIFICATION_CACHE_ITEMS:
                oldest = min(self._expires, key=self._expires.__getitem__)
                self._expires.pop(oldest, None)
            self._expires[key] = self._now() + self._ttl


def _subscription_id(principal: EventsPrincipal, name: str, arguments: dict[str, Any], url: str) -> str:
    canonical = json.dumps([principal.fingerprint, url, name, arguments], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sub_" + hashlib.sha256(canonical).hexdigest()[:64]


def _protocol_error(code: str) -> MCPError:
    # Never echo peer-supplied params or exception text into an MCP response.
    if not isinstance(code, str):
        code = ""
    if code in {"trusted_principal_missing", "binding_unavailable", "binding_owner_conflict",
                "principal_owner_conflict", "owner_confirmation_required", "permission_denied",
                "binding_already_subscribed", "binding_confirmation_required"}:
        return MCPError(code=-32024, message="Authenticated owner binding is unavailable")
    if code.startswith("invalid_") or code in {"request_id_invalid", "operation_id_invalid"}:
        return MCPError(code=-32602, message="Invalid DOTS dialogue request")
    return MCPError(code=-32024, message="DOTS dialogue request is unavailable")


def _callback_endpoint_error(exc: Exception) -> MCPError:
    """Map callback verification failures to the documented safe MCP error."""
    if isinstance(exc, TimeoutError):
        reason = "timeout"
    elif isinstance(exc, CallbackTransportError):
        reason = {
            "callback_challenge_invalid": "challenge_failed",
            "callback_challenge_rejected": "challenge_failed",
            "callback_timeout": "timeout",
            "callback_address_limit": "address_limit",
            "callback_dns_failed": "address_unavailable",
            "callback_address_not_public": "address_not_public",
            "invalid_callback_url": "invalid_url",
            "invalid_signing_secret": "invalid_signing_secret",
            "callback_response_too_large": "response_too_large",
            "callback_delivery_failed": "delivery_failed",
        }.get(exc.code, "verification_failed")
    else:
        # Do not expose exception text, peer responses, URL, or secret.
        reason = "verification_failed"
    return MCPError(code=-32015, message="DOTS callback endpoint verification failed",
                    data={"reason": reason})


class DotsDialogueEventsExtension(Extension):
    """Public MCP SDK extension. The normal AoiTalk server never installs it."""

    identifier = "com.aoitalk/dots-dialogue"

    def __init__(self, *, backend: DialogueEventsBackend | None = None,
                 callback_sender: CallbackSender | None = None,
                 principal_resolver: Callable[[Any], EventsPrincipal | None] = verified_principal_from_sdk_context,
                 verification_cache: CallbackVerificationCache | None = None,
                 enabled: bool = False, synthetic_test_context: bool = False,
                 production_gate: bool = False):
        self.backend = backend
        self.callback_sender = callback_sender
        self.principal_resolver = principal_resolver
        self.verification_cache = verification_cache or CallbackVerificationCache()
        bridge_verifies_callback = getattr(backend, "verifies_subscription_callback", False) is True
        production_resolver = is_trusted_principal_resolver(principal_resolver)
        production_backend = False
        if production_gate is True and production_resolver:
            try:
                from .dots_dialogue_http_backend import (
                    DedicatedDotsDialogueHttpBackend,
                    DotsDialogueHttpBackend,
                )
                if principal_resolver is verified_principal_from_sdk_context:
                    production_backend = (isinstance(backend, DotsDialogueHttpBackend)
                                          and backend.uses_sdk_auth_context is True)
                elif isinstance(principal_resolver, DedicatedPrincipalResolver):
                    production_backend = (
                        isinstance(backend, DedicatedDotsDialogueHttpBackend)
                        and backend.dedicated_connection_config is principal_resolver.connection_config
                        and backend.dedicated_connection_fingerprint
                        == principal_resolver.connection_config.principal.fingerprint
                    )
            except Exception:
                production_backend = False
        self.enabled = (enabled is True and (
                            synthetic_test_context is True
                            or (production_gate is True and production_resolver and production_backend))
                        and backend is not None
                        and (callback_sender is not None or bridge_verifies_callback))

    def settings(self) -> dict[str, Any]:
        return {}

    def methods(self) -> tuple[MethodBinding, ...]:
        if not self.enabled:
            return ()
        versions = frozenset({MCP_EVENTS_PROTOCOL_VERSION})
        return (
            MethodBinding("events/list", EventsListParams, self._events_list, protocol_versions=versions),
            MethodBinding("events/subscribe", EventsSubscribeParams, self._events_subscribe, protocol_versions=versions),
            MethodBinding("events/unsubscribe", EventsUnsubscribeParams, self._events_unsubscribe, protocol_versions=versions),
        )

    def tools(self) -> tuple[ToolBinding, ...]:
        if not self.enabled:
            return ()
        return (
            ToolBinding(self.aoitalk_dot_request_read,
                        kwargs={"annotations": ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                                idempotent_hint=True)}),
            ToolBinding(self.aoitalk_dot_request_reply,
                        kwargs={"annotations": ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                                idempotent_hint=True)}),
        )

    async def intercept_tool_call(self, params: CallToolRequestParams, ctx: ServerRequestContext[Any, Any],
                                  call_next: Callable[[ServerRequestContext[Any, Any]], Any]) -> Any:
        """Reject unmodeled wire fields before the SDK dispatches a tool body.

        The public SDK's function-derived outer argument model ignores extra
        top-level fields. The strict Pydantic request model below is therefore
        nested under one `request` field, and this documented Extension seam
        validates the raw `tools/call` argument object before invoking it.
        """
        if not self.enabled or params.name not in {
            "aoitalk_dot_request_read", "aoitalk_dot_request_reply",
        }:
            return await call_next(ctx)
        model = (ReadRequestToolParams if params.name.endswith("_read")
                 else ReplyRequestToolParams)
        try:
            arguments = params.arguments or {}
            if not isinstance(arguments, dict) or set(arguments) != {"request"}:
                raise ValueError("unexpected tool argument fields")
            model.model_validate(arguments["request"])
        except (ValidationError, TypeError, ValueError):
            raise MCPError(code=-32602, message="Invalid DOTS dialogue tool arguments") from None
        return await call_next(ctx)

    def _principal(self, ctx: ServerRequestContext[Any, Any]) -> EventsPrincipal:
        principal = self.principal_resolver(ctx)
        if not isinstance(principal, (VerifiedMcpPrincipal, DedicatedDotsPrincipal)):
            raise _protocol_error("trusted_principal_missing")
        try:
            _ = principal.fingerprint
        except ValueError:
            raise _protocol_error("trusted_principal_missing") from None
        return principal

    async def _events_list(self, ctx: ServerRequestContext[Any, Any], params: EventsListParams) -> dict[str, Any]:
        try:
            principal = self._principal(ctx)
            events = await self.backend.list_events(principal)
            return {"events": events}
        except MCPError:
            raise
        except Exception as exc:
            raise _protocol_error(getattr(exc, "code", "events_unavailable")) from None

    async def _events_subscribe(self, ctx: ServerRequestContext[Any, Any], params: EventsSubscribeParams) -> dict[str, Any]:
        try:
            principal = self._principal(ctx)
            arguments = dict(params.arguments)
            await self.backend.authorize_subscription(principal, params.name, arguments)
            delivery = {"mode": params.delivery.mode, "url": params.delivery.url,
                        "secret": params.delivery.secret}
            sub_id = _subscription_id(principal, params.name, arguments, params.delivery.url)
            if getattr(self.backend, "verifies_subscription_callback", False) is not True:
                if self.callback_sender is None:
                    raise _callback_endpoint_error(CallbackTransportError("callback_verification_unavailable"))
                try:
                    await self.verification_cache.verify(self.callback_sender, principal, params.delivery.url,
                                                         params.delivery.secret, sub_id)
                except Exception as exc:
                    raise _callback_endpoint_error(exc) from None
            return await self.backend.subscribe(principal, params.name, arguments, delivery,
                                                params.ttl_ms, params.cursor)
        except MCPError:
            raise
        except Exception as exc:
            raise _protocol_error(getattr(exc, "code", "subscription_unavailable")) from None

    async def _events_unsubscribe(self, ctx: ServerRequestContext[Any, Any],
                                  params: EventsUnsubscribeParams) -> dict[str, Any]:
        try:
            principal = self._principal(ctx)
            delivery = {"mode": params.delivery.mode, "url": params.delivery.url}
            result = await self.backend.unsubscribe(principal, params.name, dict(params.arguments), delivery)
            return result if isinstance(result, dict) else {}
        except MCPError:
            raise
        except Exception as exc:
            raise _protocol_error(getattr(exc, "code", "subscription_unavailable")) from None

    async def aoitalk_dot_request_read(self, request: ReadRequestToolParams,
                                       ctx: Context) -> dict[str, Any]:
        """Read only the owner message named by one event-bound delivery token."""
        try:
            result = await self.backend.read_request(
                self._principal(ctx.request_context), request.request_id, request.delivery_token,
            )
            if not isinstance(result, dict):
                raise _protocol_error("request_unavailable")
            return result
        except MCPError:
            raise
        except Exception as exc:
            raise _protocol_error(getattr(exc, "code", "request_unavailable")) from None

    async def aoitalk_dot_request_reply(self, request: ReplyRequestToolParams,
                                        ctx: Context) -> dict[str, Any]:
        """Append one correlated reply; no arbitrary conversation ID is accepted."""
        try:
            result = await self.backend.reply_request(
                self._principal(ctx.request_context), request.request_id, request.delivery_token,
                request.operation_id, request.reply_text,
            )
            if not isinstance(result, dict):
                raise _protocol_error("request_unavailable")
            return result
        except MCPError:
            raise
        except Exception as exc:
            raise _protocol_error(getattr(exc, "code", "request_unavailable")) from None


class DotsEventsCapabilityMiddleware:
    """Add the required top-level events capability after SDK serialization.

    SDK 2.3.0's standard ServerCapabilities type drops ``events``. This public
    middleware only adds it when the same live extension has all dependencies.
    """

    def __init__(self, extension: DotsDialogueEventsExtension):
        self._extension = extension

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: Callable[..., Any]) -> Any:
        result = await call_next(ctx)
        if not self._extension.enabled or ctx.method != "server/discover":
            return result
        if ctx.protocol_version != MCP_EVENTS_PROTOCOL_VERSION or not isinstance(result, dict):
            return result
        wire = dict(result)
        capabilities = wire.get("capabilities")
        if not isinstance(capabilities, dict):
            raise MCPError(code=-32603, message="MCP discovery capabilities are unavailable")
        wire["capabilities"] = {**capabilities, "events": {}}
        return wire
