"""Small Web Push delivery adapter used by the task notification worker.

The application remains fully usable when VAPID is not configured (or when
``pywebpush`` is not installed): callers receive a structured unavailable
result and retain the existing in-app/Discord delivery path.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

try:  # requests is already a project dependency (and pywebpush requires it).
    import requests
except ImportError:  # pragma: no cover - minimal fallback installations.
    requests = None  # type: ignore[assignment]


if requests is not None:

    class _NoRedirectSession(requests.Session):
        """requests session that never follows a provider redirect."""

        def request(self, method: str, url: str, **kwargs: Any):
            kwargs["allow_redirects"] = False
            return super().request(method, url, **kwargs)

else:  # pragma: no cover - requests is a pywebpush runtime dependency.
    _NoRedirectSession = None  # type: ignore[assignment,misc]

try:  # Optional import keeps local/Discord-only deployments non-breaking.
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - exercised by minimal deployments.
    WebPushException = Exception  # type: ignore[assignment,misc]
    webpush = None  # type: ignore[assignment]


@dataclass(frozen=True)
class WebPushConfig:
    private_key: str
    public_key: str
    subject: str

    @property
    def configured(self) -> bool:
        return bool(self.private_key and self.public_key and self.subject)


@dataclass(frozen=True)
class WebPushResult:
    sent: bool
    reason: str | None = None
    status_code: int | None = None


class WebPushEndpointError(ValueError):
    """Raised when a browser supplied push endpoint is not safe to contact.

    Push endpoints are user input and are later fetched by the scheduler.  A
    hostname that looks harmless can still resolve to a loopback/private
    address (or be rebound after subscription), so validation is performed at
    registration time and again immediately before delivery.
    """


_ENDPOINT_MAX_LENGTH = 4096
# Browser Push API endpoints are issued by a small, known set of providers.
# Match an exact registered suffix (or a subdomain boundary), never a loose
# ``endswith("googleapis.com")`` check that would allow attacker-controlled
# hosts such as ``fcm.googleapis.com.attacker.example``.
_WEB_PUSH_PROVIDER_HOST_SUFFIXES = (
    "fcm.googleapis.com",             # Chrome / Chromium (FCM)
    "fcmregistrations.googleapis.com",  # Chromium registration variants
    "push.services.mozilla.com",      # Firefox (including updates.*)
    "web.push.apple.com",              # Safari
    "notify.windows.com",              # Edge/WNS channel endpoints
)


def is_allowed_web_push_host(host: str | None) -> bool:
    """Return whether a hostname belongs to a supported browser push service."""

    normalized = str(host or "").strip().rstrip(".").casefold()
    if not normalized:
        return False
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in _WEB_PUSH_PROVIDER_HOST_SUFFIXES
    )


def normalize_web_push_endpoint(value: Any) -> str:
    """Validate the URL shape without doing network I/O."""

    text = str(value or "").strip()
    if (
        not text
        or len(text) > _ENDPOINT_MAX_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise WebPushEndpointError("invalid Web Push endpoint")
    try:
        parsed = urlsplit(text)
        # Accessing ``port`` rejects malformed/non-numeric ports.
        port = parsed.port
    except ValueError as exc:
        raise WebPushEndpointError("invalid Web Push endpoint") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise WebPushEndpointError("invalid Web Push endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise WebPushEndpointError("invalid Web Push endpoint")
    if address is None and (host == "localhost" or host.endswith(".local")):
        raise WebPushEndpointError("invalid Web Push endpoint")
    # Keep the original path/query (push providers commonly use query tokens),
    # but normalize a trailing DNS dot to avoid bypassing hostname checks.
    if parsed.hostname and parsed.hostname.rstrip(".").casefold() != parsed.hostname.casefold():
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        text = parsed._replace(netloc=netloc).geturl()
    return text


async def validate_web_push_endpoint(value: Any, *, timeout: float = 5.0) -> str:
    """Resolve and validate every address behind a push endpoint hostname.

    ``ipaddress.is_global`` intentionally rejects private, loopback,
    link-local, unspecified, multicast, reserved and documentation ranges.
    We fail closed on DNS errors or empty results.  Re-running this immediately
    before ``pywebpush`` mitigates DNS rebinding between subscription and send.
    """

    endpoint = normalize_web_push_endpoint(value)
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if not host:  # guarded by normalize, kept for type checkers
        raise WebPushEndpointError("invalid Web Push endpoint")
    if not is_allowed_web_push_host(host):
        raise WebPushEndpointError("unsupported Web Push provider")
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port or 443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        raise WebPushEndpointError("could not resolve Web Push endpoint") from exc
    addresses: set[str] = set()
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else None
        address_text = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
        if not address_text:
            continue
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise WebPushEndpointError("invalid resolved Web Push address") from exc
        addresses.add(str(address))
        if not address.is_global:
            raise WebPushEndpointError("Web Push endpoint resolves to a private address")
    if not addresses:
        raise WebPushEndpointError("Web Push endpoint has no resolved address")
    return endpoint


def get_web_push_config() -> WebPushConfig:
    """Read VAPID settings at call time so tests and secret rotation work."""

    return WebPushConfig(
        private_key=os.getenv("AOITALK_WEB_PUSH_VAPID_PRIVATE_KEY", "").strip(),
        public_key=os.getenv("AOITALK_WEB_PUSH_VAPID_PUBLIC_KEY", "").strip(),
        subject=(
            os.getenv("AOITALK_WEB_PUSH_VAPID_SUBJECT", "mailto:admin@localhost")
            .strip()
        ),
    )


def get_web_push_public_key() -> str | None:
    config = get_web_push_config()
    return config.public_key if config.configured else None


def _status_code_from_exception(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _send_sync(
    subscription_info: Mapping[str, Any],
    data: str,
    config: WebPushConfig,
    content_encoding: str,
) -> WebPushResult:
    if webpush is None:
        return WebPushResult(False, "pywebpush_unavailable")
    session = None
    try:
        if requests is not None:
            session = _NoRedirectSession()
        webpush(
            subscription_info=dict(subscription_info),
            data=data,
            vapid_private_key=config.private_key,
            vapid_claims={"sub": config.subject},
            content_encoding=content_encoding,
            ttl=120,
            timeout=10,
            requests_session=session,
        )
        return WebPushResult(True)
    except WebPushException as exc:  # type: ignore[misc]
        return WebPushResult(
            False,
            "push_service_error",
            _status_code_from_exception(exc),
        )
    except Exception as exc:  # pragma: no cover - provider-specific failures.
        logger.warning("Web Push delivery failed: %s", exc)
        return WebPushResult(False, "delivery_error", _status_code_from_exception(exc))
    finally:
        if session is not None:
            session.close()


async def send_web_push(
    subscription_info: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    content_encoding: str = "aes128gcm",
) -> WebPushResult:
    """Deliver one JSON notification without blocking the event loop."""

    config = get_web_push_config()
    if not config.configured:
        return WebPushResult(False, "vapid_not_configured")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return await asyncio.to_thread(
        _send_sync, subscription_info, data, config, content_encoding
    )


def is_expired_subscription_error(result: WebPushResult) -> bool:
    """410/404 means the browser removed this subscription permanently."""

    return result.status_code in {404, 410}
