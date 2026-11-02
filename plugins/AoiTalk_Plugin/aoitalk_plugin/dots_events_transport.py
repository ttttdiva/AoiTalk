"""Narrow MCP Events webhook transport with connection-time SSRF defenses.

Nothing in this module performs I/O at import. Deployments inject this sender
explicitly; tests use a fake connection factory and never open a socket.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


MAX_EVENT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 10
CALLBACK_TOTAL_TIMEOUT_SECONDS = 8.0
MAX_CALLBACK_ADDRESS_ATTEMPTS = 16
_CALLBACK_PHASES = frozenset({"validation", "dns", "tcp", "tls", "http", "deadline"})
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class CallbackTransportError(Exception):
    """Safe callback transport error; never includes URL, secret, or payload."""

    def __init__(self, code: str, status_code: int | None = None, *,
                 attempt_count: int = 0, elapsed_ms: int | None = None,
                 phase: str | None = None, origin: str | None = None,
                 address_families: Sequence[str] = (), address_count: int = 0,
                 public_addresses_validated: bool | None = None):
        self.code = code
        self.status_code = status_code if (
            isinstance(status_code, int) and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
        ) else None
        self.attempt_count = min(max(attempt_count, 0), MAX_CALLBACK_ADDRESS_ATTEMPTS)
        self.elapsed_ms = max(elapsed_ms, 0) if isinstance(elapsed_ms, int) else None
        self.phase = phase if isinstance(phase, str) and phase in _CALLBACK_PHASES else None
        self.origin = _safe_origin(origin)
        safe_families = address_families if isinstance(address_families, (tuple, list, set, frozenset)) else ()
        self.address_families = tuple(
            family for family in ("ipv4", "ipv6") if family in safe_families
        )
        self.address_count = min(max(address_count, 0), MAX_CALLBACK_ADDRESS_ATTEMPTS + 1)
        self.public_addresses_validated = (
            public_addresses_validated if isinstance(public_addresses_validated, bool) else None
        )
        super().__init__(code)


@dataclass(frozen=True)
class CallbackVerificationReceipt:
    status_code: int
    attempt_count: int
    elapsed_ms: int
    phase: str = "http"
    origin: str | None = None
    address_families: tuple[str, ...] = ()
    address_count: int = 0
    public_addresses_validated: bool | None = None


@dataclass(frozen=True)
class CallbackResponse:
    status_code: int
    body: bytes
    attempt_count: int = 1
    elapsed_ms: int = 0
    phase: str = "http"
    origin: str | None = None
    address_families: tuple[str, ...] = ()
    address_count: int = 0
    public_addresses_validated: bool | None = None


@dataclass(frozen=True)
class _CallbackTarget:
    host: str
    port: int
    request_target: str
    addresses: tuple[str, ...]
    origin: str
    address_families: tuple[str, ...]


def _safe_origin(value: Any) -> str | None:
    """Accept only a normalized HTTPS origin; never retain URL path or credentials."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        raw_host = parsed.hostname
        try:
            host = ipaddress.ip_address(raw_host).compressed.lower()
            if ":" in host:
                host = f"[{host}]"
        except ValueError:
            host = raw_host.encode("idna").decode("ascii").lower().rstrip(".")
            if len(host) > 253 or not host or any(not _DNS_LABEL.fullmatch(label) for label in host.split(".")):
                return None
        port = parsed.port or 443
        if not 1 <= port <= 65535:
            return None
        return f"https://{host}:{port}"
    except (UnicodeError, TypeError, ValueError):
        return None


class CallbackSender:
    async def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> CallbackResponse:
        raise NotImplementedError


class DeliveryOutboxBackend:
    async def lease_next_delivery(self) -> Any:
        raise NotImplementedError

    async def mark_delivery_result(self, outbox_id: str, lease_id: str, http_status: int,
                                   error_code: str | None = None) -> str:
        raise NotImplementedError


def _decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise CallbackTransportError("invalid_signing_secret")
    try:
        raw = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise CallbackTransportError("invalid_signing_secret") from None
    if not 24 <= len(raw) <= 64:
        raise CallbackTransportError("invalid_signing_secret")
    return raw


def sign_webhook(secret: str | Sequence[str], webhook_id: str, timestamp: int, body: bytes) -> str:
    secrets_to_sign = [secret] if isinstance(secret, str) else list(secret)
    if not secrets_to_sign or len(secrets_to_sign) > 2:
        raise CallbackTransportError("invalid_signing_secret")
    signed = str(webhook_id).encode("ascii") + b"." + str(timestamp).encode("ascii") + b"." + body
    signatures = []
    for item in secrets_to_sign:
        key = _decode_secret(item)
        signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
        signatures.append("v1," + signature)
    return " ".join(signatures)


def signed_headers(secret: str | Sequence[str], webhook_id: str, subscription_id: str, body: bytes,
                   *, timestamp: int | None = None) -> dict[str, str]:
    if len(body) > MAX_EVENT_BYTES:
        raise CallbackTransportError("event_too_large")
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    return {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(stamp),
        "webhook-signature": sign_webhook(secret, webhook_id, stamp, body),
        "X-MCP-Subscription-Id": subscription_id,
    }


def _callback_target(url: str, resolver: Callable[..., Any]) -> _CallbackTarget:
    if not isinstance(url, str) or len(url) > 2048:
        raise CallbackTransportError("invalid_callback_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError
        origin = _safe_origin(url)
        if origin is None:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port or 443
        if not 1 <= port <= 65535 or "%" in host:
            raise ValueError
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
    except (UnicodeError, TypeError, ValueError):
        raise CallbackTransportError("invalid_callback_url", phase="validation") from None

    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            records = resolver(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records]
        except Exception:
            raise CallbackTransportError(
                "callback_dns_failed", phase="dns", origin=origin,
                public_addresses_validated=False,
            ) from None
    families = tuple(
        family for family, version in (("ipv4", 4), ("ipv6", 6))
        if any(address.version == version for address in addresses)
    )
    address_count = min(len(set(addresses)), MAX_CALLBACK_ADDRESS_ATTEMPTS + 1)
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise CallbackTransportError(
            "callback_address_not_public", phase="dns", origin=origin,
            address_families=families, address_count=address_count,
            public_addresses_validated=False,
        )
    # Keep the DNS answer fixed through connect; never hand the hostname back to
    # an HTTP library which could resolve it again after this check.
    return _CallbackTarget(
        host=host, port=port, request_target=target,
        addresses=tuple(dict.fromkeys(str(address) for address in addresses)),
        origin=origin, address_families=families,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, *, timeout: float,
                 context: ssl.SSLContext, deadline: float | None = None,
                 cancel_event: threading.Event | None = None,
                 socket_factory: Callable[..., Any] | None = None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._validated_address = address
        self._deadline = deadline
        self._cancel_event = cancel_event
        self._socket_factory = socket_factory or socket.socket
        self.callback_phase = "tcp"

    def _remaining_timeout(self) -> float:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ConnectionAbortedError("callback operation cancelled")
        if self._deadline is None:
            return self.timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("callback operation deadline expired")
        return min(self.timeout, remaining)

    def _check_active(self) -> None:
        self._remaining_timeout()

    def connect(self) -> None:
        self.callback_phase = "tcp"
        parsed_address = ipaddress.ip_address(self._validated_address)
        family = socket.AF_INET if parsed_address.version == 4 else socket.AF_INET6
        destination = ((self._validated_address, self.port) if family == socket.AF_INET
                       else (self._validated_address, self.port, 0, 0))
        raw = self._socket_factory(family, socket.SOCK_STREAM)
        self.sock = raw
        try:
            raw.settimeout(self._remaining_timeout())
            raw.connect(destination)
            self._check_active()
            raw.settimeout(self._remaining_timeout())
            self.callback_phase = "tls"
            secure = self._context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False,
            )
            self.sock = secure
            secure.settimeout(self._remaining_timeout())
            self._check_active()
            secure.do_handshake()
            self._check_active()
            self.callback_phase = "http"
        except BaseException:
            owned = self.sock
            self.sock = None
            if owned is not None:
                owned.close()
            if owned is not raw:
                raw.close()
            raise

    def send(self, data: Any) -> None:
        self._check_active()
        # http.client normally connects and writes in one call. Split the
        # connect from the write so a TCP/TLS phase that consumed the deadline
        # can never resume and emit the request afterward.
        if self.sock is None:
            if not self.auto_open:
                raise http.client.NotConnected()
            self.connect()
        self._check_active()
        if self.sock is not None:
            self.sock.settimeout(self._remaining_timeout())
        self._check_active()
        self.callback_phase = "http"
        super().send(data)
        self._check_active()

    def abort(self) -> None:
        """Interrupt an in-flight socket operation when its total deadline expires."""
        sock = self.sock
        if sock is None:
            self.close()
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        self.sock = None


class _CallbackOperation:
    """Shared total deadline and cancellation signal for one callback POST."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._connection: Any | None = None
        self.attempt_count = 0
        self._origin: str | None = None
        self._address_families: tuple[str, ...] = ()
        self._address_count = 0
        self._public_addresses_validated: bool | None = None
        self._phase = "dns"

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def attach(self, connection: Any) -> None:
        with self._lock:
            self._connection = connection
        if self.cancelled.is_set() or self.expired.is_set():
            self._abort(connection)

    def set_target(self, target: _CallbackTarget) -> None:
        with self._lock:
            self._origin = target.origin
            self._address_families = target.address_families
            self._address_count = min(len(target.addresses), MAX_CALLBACK_ADDRESS_ATTEMPTS + 1)
            self._public_addresses_validated = True
            self._phase = "tcp"

    def set_phase(self, phase: str) -> None:
        if isinstance(phase, str) and phase in _CALLBACK_PHASES:
            with self._lock:
                self._phase = phase

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            connection = self._connection
            phase = getattr(connection, "callback_phase", self._phase) if connection is not None else self._phase
            return {
                "phase": phase if isinstance(phase, str) and phase in _CALLBACK_PHASES else self._phase,
                "origin": self._origin,
                "address_families": self._address_families,
                "address_count": self._address_count,
                "public_addresses_validated": self._public_addresses_validated,
            }

    def detach(self, connection: Any) -> None:
        with self._lock:
            if self._connection is connection:
                phase = getattr(connection, "callback_phase", None)
                if isinstance(phase, str) and phase in _CALLBACK_PHASES:
                    self._phase = phase
                self._connection = None

    @staticmethod
    def _abort(connection: Any) -> None:
        try:
            abort = getattr(connection, "abort", None)
            if callable(abort):
                abort()
            else:
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
                else:
                    connection.close()
        except Exception:
            # Socket close races are expected when a response completes as a
            # deadline or cancellation arrives. Never surface exception text.
            pass

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            self._abort(connection)

    def expire(self) -> None:
        self.expired.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            self._abort(connection)


class SecureHttpsCallbackSender(CallbackSender):
    """HTTPS-only sender: re-resolves each request, pins a public IP, and never follows redirects."""

    def __init__(self, *, resolver: Callable[..., Any] = socket.getaddrinfo,
                 connection_factory: Callable[..., http.client.HTTPSConnection] = _PinnedHTTPSConnection,
                 timeout: float = CALLBACK_TOTAL_TIMEOUT_SECONDS):
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._timeout = min(max(float(timeout), 0.1), CALLBACK_TOTAL_TIMEOUT_SECONDS)

    async def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> CallbackResponse:
        if len(body) > MAX_EVENT_BYTES:
            raise CallbackTransportError("event_too_large")
        started = time.monotonic()
        operation = _CallbackOperation(started + self._timeout)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._post_sync, url, bytes(body), dict(headers), operation, started,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            operation.expire()
            raise CallbackTransportError(
                "callback_timeout", attempt_count=operation.attempt_count,
                elapsed_ms=_elapsed_ms(started), **operation.diagnostics(),
            ) from None
        except CallbackTransportError as exc:
            diagnostics = operation.diagnostics()
            raise CallbackTransportError(
                exc.code, exc.status_code,
                attempt_count=exc.attempt_count or operation.attempt_count,
                elapsed_ms=exc.elapsed_ms if exc.elapsed_ms is not None else _elapsed_ms(started),
                phase=exc.phase or diagnostics["phase"],
                origin=exc.origin or diagnostics["origin"],
                address_families=exc.address_families or diagnostics["address_families"],
                address_count=exc.address_count or diagnostics["address_count"],
                public_addresses_validated=(
                    exc.public_addresses_validated if exc.public_addresses_validated is not None
                    else diagnostics["public_addresses_validated"]
                ),
            ) from None
        except asyncio.CancelledError:
            operation.cancel()
            raise

    def _post_sync(self, url: str, body: bytes, headers: dict[str, str],
                   operation: _CallbackOperation, started: float) -> CallbackResponse:
        if operation.cancelled.is_set():
            raise CallbackTransportError("callback_cancelled", elapsed_ms=_elapsed_ms(started))
        if operation.expired.is_set() or operation.remaining() <= 0:
            raise CallbackTransportError("callback_timeout", elapsed_ms=_elapsed_ms(started))
        try:
            target_info = _callback_target(url, self._resolver)
            operation.set_target(target_info)
        except CallbackTransportError as exc:
            diagnostics = operation.diagnostics()
            raise CallbackTransportError(
                exc.code, exc.status_code, attempt_count=0,
                elapsed_ms=_elapsed_ms(started), phase=exc.phase,
                origin=exc.origin, address_families=exc.address_families,
                address_count=exc.address_count,
                public_addresses_validated=exc.public_addresses_validated,
            ) from None
        host, port, target, addresses = (
            target_info.host, target_info.port, target_info.request_target, target_info.addresses,
        )
        if len(addresses) > MAX_CALLBACK_ADDRESS_ATTEMPTS:
            raise CallbackTransportError(
                "callback_address_limit", attempt_count=0,
                elapsed_ms=_elapsed_ms(started), phase="dns", origin=target_info.origin,
                address_families=target_info.address_families,
                address_count=len(addresses), public_addresses_validated=True,
            )
        if operation.expired.is_set() or operation.remaining() <= 0:
            raise CallbackTransportError("callback_timeout", elapsed_ms=_elapsed_ms(started))
        last_error: Exception | None = None
        attempt_count = 0
        for address in addresses[:MAX_CALLBACK_ADDRESS_ATTEMPTS]:
            if operation.cancelled.is_set():
                raise CallbackTransportError(
                    "callback_cancelled", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started),
                )
            if operation.expired.is_set() or operation.remaining() <= 0:
                raise CallbackTransportError(
                    "callback_timeout", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started),
                )
            connection = None
            completed_response = None
            try:
                remaining = operation.remaining()
                if remaining <= 0:
                    raise CallbackTransportError("callback_timeout")
                connection = self._connection_factory(
                    host, port, address, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining),
                    context=ssl.create_default_context(),
                    deadline=operation.deadline,
                    cancel_event=operation.cancelled,
                )
                operation.attach(connection)
                operation.set_phase("tcp")
                remaining = operation.remaining()
                if remaining <= 0:
                    raise CallbackTransportError("callback_timeout")
                attempt_count += 1
                operation.attempt_count = attempt_count
                operation.set_phase("http")
                connection.request("POST", target, body=body, headers=headers)
                response = connection.getresponse()
                result_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(result_body) > MAX_RESPONSE_BYTES:
                    raise CallbackTransportError(
                        "callback_response_too_large", response.status,
                        attempt_count=attempt_count, elapsed_ms=_elapsed_ms(started),
                        **operation.diagnostics(),
                    )
                if operation.expired.is_set() or operation.remaining() <= 0:
                    raise CallbackTransportError(
                        "callback_timeout", attempt_count=attempt_count,
                        elapsed_ms=_elapsed_ms(started), **operation.diagnostics(),
                    )
                completed_response = CallbackResponse(
                    int(response.status), result_body, attempt_count,
                    _elapsed_ms(started), **operation.diagnostics(),
                )
            except CallbackTransportError as exc:
                if exc.code == "callback_response_too_large":
                    raise CallbackTransportError(
                        exc.code, exc.status_code, attempt_count=attempt_count,
                        elapsed_ms=_elapsed_ms(started),
                    ) from None
                last_error = exc
            except TimeoutError:
                diagnostics = operation.diagnostics()
                last_error = CallbackTransportError(
                    "callback_timeout", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started), **diagnostics,
                )
            except (OSError, ssl.SSLError, http.client.HTTPException):
                diagnostics = operation.diagnostics()
                last_error = CallbackTransportError(
                    "callback_delivery_failed", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started), **diagnostics,
                )
            except Exception:
                diagnostics = operation.diagnostics()
                last_error = CallbackTransportError(
                    "callback_delivery_failed", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started), **diagnostics,
                )
            finally:
                if connection is not None:
                    operation.detach(connection)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if completed_response is not None:
                if operation.expired.is_set() or operation.remaining() <= 0:
                    raise CallbackTransportError(
                        "callback_timeout", attempt_count=attempt_count,
                        elapsed_ms=_elapsed_ms(started), **operation.diagnostics(),
                    )
                return completed_response
            if operation.cancelled.is_set():
                raise CallbackTransportError(
                    "callback_cancelled", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started),
                )
            if operation.expired.is_set() or operation.remaining() <= 0:
                raise CallbackTransportError(
                    "callback_timeout", attempt_count=attempt_count,
                    elapsed_ms=_elapsed_ms(started),
                )
        error = last_error or CallbackTransportError("callback_delivery_failed")
        raise CallbackTransportError(
            error.code, error.status_code, attempt_count=attempt_count,
            elapsed_ms=_elapsed_ms(started), phase=error.phase or operation.diagnostics()["phase"],
            origin=error.origin or operation.diagnostics()["origin"],
            address_families=error.address_families or operation.diagnostics()["address_families"],
            address_count=error.address_count or operation.diagnostics()["address_count"],
            public_addresses_validated=(
                error.public_addresses_validated if error.public_addresses_validated is not None
                else operation.diagnostics()["public_addresses_validated"]
            ),
        ) from None


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


async def verify_callback(sender: CallbackSender, *, url: str, secret: str,
                          subscription_id: str) -> CallbackVerificationReceipt:
    """Perform OpenAI's signed one-time callback challenge before activation."""
    started = time.monotonic()
    challenge = secrets.token_urlsafe(32)
    webhook_id = "msg_verification_" + uuid.uuid4().hex
    body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
    headers = signed_headers(secret, webhook_id, subscription_id, body)
    try:
        response = await sender.post(url, body, headers)
    except CallbackTransportError as exc:
        raise CallbackTransportError(
            exc.code, exc.status_code,
            attempt_count=exc.attempt_count,
            elapsed_ms=exc.elapsed_ms if exc.elapsed_ms is not None else _elapsed_ms(started),
            phase=exc.phase,
            origin=exc.origin,
            address_families=exc.address_families,
            address_count=exc.address_count,
            public_addresses_validated=exc.public_addresses_validated,
        ) from None
    if not 200 <= response.status_code < 300:
        raise CallbackTransportError(
            "callback_challenge_rejected", response.status_code,
            attempt_count=response.attempt_count, elapsed_ms=response.elapsed_ms,
        )
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise CallbackTransportError(
            "callback_response_too_large", response.status_code,
            attempt_count=response.attempt_count, elapsed_ms=response.elapsed_ms,
        )
    try:
        returned = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CallbackTransportError(
            "callback_challenge_invalid", response.status_code,
            attempt_count=response.attempt_count, elapsed_ms=response.elapsed_ms,
        ) from None
    if not isinstance(returned, dict) or not isinstance(returned.get("challenge"), str) or not hmac.compare_digest(
            returned["challenge"], challenge):
        raise CallbackTransportError(
            "callback_challenge_invalid", response.status_code,
            attempt_count=response.attempt_count, elapsed_ms=response.elapsed_ms,
        )
    return CallbackVerificationReceipt(
        status_code=response.status_code,
        attempt_count=min(max(response.attempt_count, 0), MAX_CALLBACK_ADDRESS_ATTEMPTS),
        elapsed_ms=max(response.elapsed_ms, _elapsed_ms(started)),
        phase=response.phase,
        origin=response.origin,
        address_families=response.address_families,
        address_count=min(max(response.address_count, 0), MAX_CALLBACK_ADDRESS_ATTEMPTS + 1),
        public_addresses_validated=response.public_addresses_validated,
    )


async def deliver_event(sender: CallbackSender, *, url: str, secret: str,
                        previous_secret: str | None = None, subscription_id: str,
                        event_id: str, body: bytes) -> int:
    secrets_to_sign = (secret, previous_secret) if previous_secret else secret
    headers = signed_headers(secrets_to_sign, event_id, subscription_id, body)
    response = await sender.post(url, body, headers)
    return response.status_code


async def dispatch_one_pending(backend: DeliveryOutboxBackend, sender: CallbackSender) -> str:
    """Deliver at most one leased row; a caller controls scheduling and activation."""
    lease = await backend.lease_next_delivery()
    if lease is None:
        return "idle"
    status = 0
    error = None
    try:
        status = await deliver_event(
            sender,
            url=lease.callback_url,
            secret=lease.signing_secret,
            previous_secret=lease.previous_signing_secret,
            subscription_id=lease.subscription_id,
            event_id=lease.event_id,
            body=lease.body,
        )
    except CallbackTransportError as exc:
        status, error = exc.status_code or 0, exc.code
    except Exception:
        # Do not propagate raw transport diagnostics: they can contain callback
        # data or response text and are not useful as a retry classification.
        status, error = 0, "callback_delivery_failed"
    return await backend.mark_delivery_result(str(lease.outbox_id), str(lease.lease_id), status, error)
