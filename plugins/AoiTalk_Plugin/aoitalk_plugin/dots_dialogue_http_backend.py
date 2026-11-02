"""HTTP adapter for the default-off AoiTalk DOTS bridge.

No caller identity is serialized in JSON or custom headers.  The remote bridge
must validate the bearer token for its exact resource audience and derive the
principal itself.  This adapter accepts only the active MCP SDK access-token
context, checks its resource and verified principal against the caller supplied
by the Events extension, and makes one HTTP attempt per operation.  Stdio and
Tunnel runtimes without that OAuth context fail closed.
"""
from __future__ import annotations

import inspect
import ipaddress
import json
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx


MAX_BRIDGE_RESPONSE_BYTES = 256 * 1024
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 10.0


class DotsDialogueBackendError(RuntimeError):
    """Safe bridge error; never includes credentials, URLs, or response text."""

    def __init__(self, code: str = "bridge_unavailable", status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class PrincipalLike(Protocol):
    client_id: str
    issuer: str
    subject: str


def _sdk_principal_components(access_token: Any) -> tuple[str, str | None, str | None]:
    # Use the installed MCP SDK's canonical token identity helper.  It only
    # consumes an AccessToken returned from the SDK's validated auth context.
    from mcp.server.auth.provider import principal_components

    return principal_components(access_token)


def _safe_remote_code(value: Any) -> str:
    allowed = {
        "dots_dialogue_disabled", "trusted_principal_unavailable", "trusted_principal_missing",
        "invalid_subscription", "invalid_binding_id", "binding_unavailable", "request_unavailable",
        "request_not_delivered", "invalid_reply", "invalid_operation_id", "operation_conflict",
        "reply_already_accepted", "delivery_lease_stale",
        "conversation_unavailable", "active_subscription_required", "cursor_unavailable",
        "subscription_unavailable", "events_unavailable", "invalid_message", "invalid_idempotency_key",
        "owner_confirmation_required", "principal_owner_conflict", "binding_confirmation_required",
        "dedicated_conversation_immutable",
        "dots_dialogue_not_selected",
        "callback_verification_unavailable", "callback_challenge_failed", "callback_challenge_rejected",
        "callback_challenge_invalid", "callback_timeout", "callback_address_limit",
        "callback_dns_failed", "callback_address_not_public",
        "invalid_callback_url", "invalid_signing_secret", "callback_response_too_large",
    }
    return value if isinstance(value, str) and value in allowed else "bridge_request_failed"


class DotsDialogueHttpBackend:
    """Implement the MCP Events backend protocol over the AoiTalk HTTP bridge.

    ``access_token_provider`` should be the MCP SDK's ``get_access_token`` (or
    an equivalent host callback that returns a token from the SDK's validated
    authentication context).  It is intentionally optional so an unconfigured
    stdio/Tunnel plugin constructs a closed adapter rather than inventing a
    credential.

    The bridge's ``subscriptions`` endpoint performs the callback challenge
    before persisting a subscription.  This explicit capability lets the
    Events extension select the bridge as the single authoritative verifier;
    direct callers still receive the same challenge because the bridge keeps
    that check mandatory.
    """

    verifies_subscription_callback = True

    def __init__(self, *, base_url: str, bridge_audience: str,
                 access_token_provider: Callable[[], Any] | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 principal_components: Callable[[Any], tuple[str, str | None, str | None]] | None = None,
                 timeout_seconds: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS):
        if not isinstance(base_url, str):
            raise ValueError("base_url must be an HTTPS origin")
        parsed = urlsplit(base_url)
        is_loopback_http = False
        if parsed.scheme.lower() == "http" and parsed.hostname:
            try:
                is_loopback_http = parsed.hostname.lower() == "localhost" or ipaddress.ip_address(
                    parsed.hostname
                ).is_loopback
            except ValueError:
                is_loopback_http = False
        if ((parsed.scheme.lower() != "https" and not is_loopback_http) or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("base_url must be HTTPS or loopback HTTP")
        if not isinstance(bridge_audience, str) or not bridge_audience.strip() or len(bridge_audience) > 512:
            raise ValueError("bridge_audience is required")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._bridge_audience = bridge_audience.rstrip("/")
        self._access_token_provider = access_token_provider
        self._http_client = http_client
        self._principal_components = principal_components or _sdk_principal_components
        self._timeout_seconds = float(timeout_seconds)

    @property
    def bridge_audience(self) -> str:
        """The exact resource audience used for every bridge bearer token."""
        return self._bridge_audience

    @property
    def uses_sdk_auth_context(self) -> bool:
        """Whether credentials are read from the MCP SDK request context."""
        from mcp.server.auth.middleware.auth_context import get_access_token

        return self._access_token_provider is get_access_token

    async def _credential(self, principal: PrincipalLike) -> str:
        provider = self._access_token_provider
        if provider is None:
            raise DotsDialogueBackendError("trusted_principal_unavailable", 503)
        try:
            access_token = provider()
            if inspect.isawaitable(access_token):
                access_token = await access_token
            token_value = getattr(access_token, "token", None)
            resource = getattr(access_token, "resource", None)
            if (not isinstance(token_value, str) or not token_value or len(token_value) > 16_384
                    or resource != self._bridge_audience):
                raise DotsDialogueBackendError("trusted_principal_unavailable", 503)
            client_id, issuer, subject = self._principal_components(access_token)
            expected = (getattr(principal, "client_id", None), getattr(principal, "issuer", None),
                        getattr(principal, "subject", None))
            verified = (client_id, issuer, subject)
            if (any(not isinstance(part, str) or not part.strip() for part in verified)
                    or verified != expected):
                raise DotsDialogueBackendError("trusted_principal_missing", 403)
            return token_value
        except DotsDialogueBackendError:
            raise
        except Exception:
            raise DotsDialogueBackendError("trusted_principal_unavailable", 503) from None

    async def _request(self, method: str, path: str, principal: PrincipalLike,
                       payload: dict[str, Any] | None = None) -> Any:
        token = await self._credential(principal)
        url = self._base_url + "/api/dots/dialogue/bridge" + path
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            )
        try:
            async with client.stream(method, url, headers=headers, json=payload,
                                     follow_redirects=False, timeout=self._timeout_seconds) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BRIDGE_RESPONSE_BYTES:
                        raise DotsDialogueBackendError("bridge_response_too_large", response.status_code)
                try:
                    decoded = json.loads(bytes(raw).decode("utf-8")) if raw else {}
                except (UnicodeError, ValueError):
                    raise DotsDialogueBackendError("bridge_invalid_response", response.status_code) from None
                if not 200 <= response.status_code < 300:
                    code = None
                    if isinstance(decoded, dict):
                        detail = decoded.get("detail")
                        if isinstance(detail, dict):
                            code = detail.get("code")
                    raise DotsDialogueBackendError(_safe_remote_code(code), response.status_code)
                return decoded
        except DotsDialogueBackendError:
            raise
        except Exception:
            # HTTPX exception messages can include the full URL.  Never return
            # them through the MCP surface or log token-bearing request state.
            raise DotsDialogueBackendError("bridge_unavailable") from None
        finally:
            if owns_client:
                await client.aclose()

    async def list_events(self, principal: PrincipalLike) -> list[dict[str, Any]]:
        response = await self._request("GET", "/events", principal)
        events = response.get("events") if isinstance(response, dict) else None
        if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
            raise DotsDialogueBackendError("bridge_invalid_response")
        return events

    async def authorize_subscription(self, principal: PrincipalLike, name: str,
                                     arguments: dict[str, Any]) -> None:
        await self._request("POST", "/subscriptions/authorize", principal,
                            {"name": name, "arguments": arguments})

    async def subscribe(self, principal: PrincipalLike, name: str, arguments: dict[str, Any],
                        delivery: dict[str, str], ttl_ms: int | None,
                        cursor: str | None) -> dict[str, Any]:
        response = await self._request("POST", "/subscriptions", principal, {
            "name": name,
            "arguments": arguments,
            "delivery": delivery,
            "ttl_ms": ttl_ms,
            "cursor": cursor,
        })
        if not isinstance(response, dict):
            raise DotsDialogueBackendError("bridge_invalid_response")
        return response

    async def unsubscribe(self, principal: PrincipalLike, name: str, arguments: dict[str, Any],
                          delivery: dict[str, str]) -> dict[str, Any]:
        response = await self._request("POST", "/subscriptions/unsubscribe", principal, {
            "name": name, "arguments": arguments, "delivery": delivery,
        })
        if not isinstance(response, dict):
            raise DotsDialogueBackendError("bridge_invalid_response")
        return response

    async def read_request(self, principal: PrincipalLike, request_id: str,
                           delivery_token: str) -> dict[str, Any]:
        try:
            request_uuid = str(UUID(str(request_id)))
        except (TypeError, ValueError, AttributeError):
            raise DotsDialogueBackendError("request_unavailable", 404) from None
        response = await self._request("POST", f"/requests/{request_uuid}/read", principal,
                                       {"delivery_token": delivery_token})
        if not isinstance(response, dict):
            raise DotsDialogueBackendError("bridge_invalid_response")
        return response

    async def reply_request(self, principal: PrincipalLike, request_id: str,
                            delivery_token: str, operation_id: str,
                            reply_text: str) -> dict[str, Any]:
        try:
            request_uuid = str(UUID(str(request_id)))
        except (TypeError, ValueError, AttributeError):
            raise DotsDialogueBackendError("request_unavailable", 404) from None
        response = await self._request("POST", f"/requests/{request_uuid}/reply", principal, {
            "delivery_token": delivery_token,
            "operation_id": operation_id,
            "reply_text": reply_text,
        })
        if not isinstance(response, dict):
            raise DotsDialogueBackendError("bridge_invalid_response")
        return response


class DedicatedDotsDialogueHttpBackend(DotsDialogueHttpBackend):
    """Use one server-owned private capability over the existing bridge.

    This carries no OAuth claims.  It is only constructed from the fixed
    dedicated Plugin/Tunnel configuration, and it only accepts that
    configuration's principal on every operation.
    """

    def __init__(self, *, connection_config: Any,
                 http_client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS):
        from .dots_dialogue_dedicated import DedicatedDotsConnectionConfig

        if not isinstance(connection_config, DedicatedDotsConnectionConfig):
            raise ValueError("dedicated connection config is required")
        bridge_audience = connection_config.api_base_url.rstrip("/") + "/api/dots/dialogue/bridge"
        super().__init__(
            base_url=connection_config.api_base_url,
            bridge_audience=bridge_audience,
            access_token_provider=None,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
        )
        self._dedicated_config = connection_config

    @property
    def dedicated_connection_fingerprint(self) -> str:
        return self._dedicated_config.principal.fingerprint

    @property
    def dedicated_connection_config(self) -> Any:
        return self._dedicated_config

    async def _credential(self, principal: PrincipalLike) -> str:
        from .dots_dialogue_dedicated import DedicatedDotsPrincipal

        configured = self._dedicated_config.principal
        if (not isinstance(principal, DedicatedDotsPrincipal)
                or principal.fingerprint != configured.fingerprint
                or principal.conversation_id != configured.conversation_id):
            raise DotsDialogueBackendError("trusted_principal_missing", 403)
        return self._dedicated_config.capability


__all__ = [
    "DedicatedDotsDialogueHttpBackend",
    "DotsDialogueBackendError",
    "DotsDialogueHttpBackend",
]
