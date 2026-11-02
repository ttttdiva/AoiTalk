"""Authenticated control-IPC client for the AoiTalk Edge broker.

This client only talks to AoiTalk's same-user named pipe. It never opens
Edge CDP itself. Browser endpoints are held in a redacted wrapper so accidental
repr/str logging cannot reveal the private Playwright WebSocket path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
import multiprocessing.connection
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit


_APP_DIRECTORY = "AoiTalk/browser-broker"
_CONTROL_FILE = "control.json"
_AUTHKEY_FILE = "control.authkey"
_CONTROL_PROTOCOL_VERSION = 1
_MAX_TIMEOUT_SEC = 60.0
_PHASES = {"idle", "connecting", "ready", "failed", "reset_required"}
_SAFE_ERROR_CODE = re.compile(r"^edge_broker_[a-z0-9_]{1,64}$")
_SAFE_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9 ._/-]{1,200}$")
_PIPE_SUFFIX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class EdgeBrokerControlError(RuntimeError):
    """Fixed-code error that never embeds pipe, authkey, or endpoint values."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _SAFE_ERROR_CODE.fullmatch(code):
            code = "edge_broker_unavailable"
        super().__init__(code)
        self.code = code


def _raise(code: str) -> None:
    raise EdgeBrokerControlError(code) from None


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    """Validated, value-safe broker diagnostics."""

    phase: str
    error_code: str | None
    error_at_utc: str | None
    cdp_connected_success: bool | None
    cdp_connected: bool
    broker_pid: int | None
    status_ipc_supported: bool
    mode: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class BrokerBrowserEndpoint:
    """Opaque endpoint holder; only ``for_connection`` exposes its value."""

    _value: str = field(repr=False)

    def for_connection(self) -> str:
        """Return the endpoint for an immediate in-memory Playwright connect."""
        return self._value

    def __repr__(self) -> str:
        return "BrokerBrowserEndpoint(<redacted>)"

    def __str__(self) -> str:
        return "BrokerBrowserEndpoint(<redacted>)"


def _local_pipe_suffix() -> str:
    identity = "\\".join(
        (
            os.environ.get("USERDOMAIN", ""),
            os.environ.get("USERNAME", ""),
            os.environ.get("LOCALAPPDATA", ""),
        )
    )
    return hashlib.sha256(identity.casefold().encode("utf-8", "replace")).hexdigest()[:24]


def _expected_pipe_address() -> str:
    return rf"\\.\pipe\LOCAL\AoiTalk.BrowserBroker.{_local_pipe_suffix()}"


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
    except OSError:
        _raise("edge_broker_unavailable")
    if len(value) > limit:
        _raise("edge_broker_invalid_control")
    return value


def _read_control_config() -> tuple[str, bytes]:
    """Read AoiTalk's bounded control pointer and per-user authkey."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data or not Path(local_app_data).is_absolute():
        _raise("edge_broker_storage_unavailable")
    directory = Path(local_app_data) / _APP_DIRECTORY
    raw = _read_bounded(directory / _CONTROL_FILE, 2_048)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise("edge_broker_invalid_control")
    address = payload.get("pipe") if isinstance(payload, dict) else None
    suffix = address.rsplit("\\", 1)[-1] if isinstance(address, str) else ""
    prefix = "\\\\.\\pipe\\LOCAL\\"
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _CONTROL_PROTOCOL_VERSION
        or not isinstance(address, str)
        or len(address) > 240
        or not address.casefold().startswith(prefix.casefold())
        or not _PIPE_SUFFIX.fullmatch(suffix)
        or address.casefold() != _expected_pipe_address().casefold()
    ):
        _raise("edge_broker_invalid_control")
    authkey = _read_bounded(directory / _AUTHKEY_FILE, 64)
    if len(authkey) != 32:
        _raise("edge_broker_invalid_control")
    return address, authkey


def _request_sync(
    address: str,
    authkey: bytes,
    request: dict[str, Any],
    wait_sec: float,
) -> Any:
    """Perform one authenticated named-pipe request using Gateway's protocol."""
    connection = None
    try:
        connection = multiprocessing.connection.Client(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        connection.send(request)
        if not connection.poll(wait_sec + 1.0):
            _raise("edge_broker_timeout")
        return connection.recv()
    finally:
        if connection is not None:
            connection.close()


def _validate_timeout(timeout_sec: float) -> float:
    if (
        type(timeout_sec) not in (int, float)
        or not math.isfinite(timeout_sec)
        or not 1 <= timeout_sec <= _MAX_TIMEOUT_SEC
    ):
        _raise("edge_broker_invalid_timeout")
    return float(timeout_sec)


def _validated_status(response: Any) -> BrokerStatus:
    if not isinstance(response, dict):
        _raise("edge_broker_invalid_response")
    if response.get("phase") == "failed" and response.get("error") == "edge_broker_invalid_request":
        _raise("edge_broker_status_unsupported")
    if response.get("status_ipc_supported") is False:
        _raise("edge_broker_status_unsupported")

    phase = response.get("phase")
    error_code = response.get("error_code")
    error_at_utc = response.get("error_at_utc")
    cdp_success = response.get("cdp_connected_success")
    cdp_connected = response.get("cdp_connected")
    broker_pid = response.get("broker_pid")
    mode = response.get("mode")
    if (
        phase not in _PHASES
        or (error_code is not None and (not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code)))
        or (
            error_at_utc is not None
            and (not isinstance(error_at_utc, str) or not _SAFE_UTC_TIMESTAMP.fullmatch(error_at_utc))
        )
        or (cdp_success is not None and type(cdp_success) is not bool)
        or type(cdp_connected) is not bool
        or (broker_pid is not None and (type(broker_pid) is not int or broker_pid <= 0))
        or (mode is not None and (not isinstance(mode, str) or mode not in {"dedicated", "legacy"}))
        or response.get("status_ipc_supported") is not True
    ):
        _raise("edge_broker_invalid_response")
    return BrokerStatus(
        phase=phase,
        error_code=error_code,
        error_at_utc=error_at_utc,
        cdp_connected_success=cdp_success,
        cdp_connected=cdp_connected,
        broker_pid=broker_pid,
        status_ipc_supported=True,
        mode=mode,
    )


def _validate_loopback_endpoint(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        _raise("edge_broker_invalid_endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _raise("edge_broker_invalid_endpoint")
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Fa-f0-9]{32}", parsed.path)
    ):
        _raise("edge_broker_invalid_endpoint")
    return value


class EdgeBrokerControlClient:
    """Read broker readiness and request its existing Browser.bind endpoint.

    ``config_reader`` and ``requester`` exist as narrow fake-IPC seams for
    deterministic tests. Production callers should use the defaults.
    """

    def __init__(
        self,
        *,
        config_reader: Callable[[], tuple[str, bytes]] | None = None,
        requester: Callable[[str, bytes, dict[str, Any], float], Any] | None = None,
    ) -> None:
        self._config_reader = config_reader or _read_control_config
        self._requester = requester or _request_sync

    async def _request(self, request: dict[str, Any], timeout_sec: float) -> Any:
        if os.name != "nt" and self._requester is _request_sync:
            _raise("edge_broker_windows_required")
        try:
            address, authkey = await asyncio.to_thread(self._config_reader)
            return await asyncio.wait_for(
                asyncio.to_thread(self._requester, address, authkey, request, timeout_sec),
                timeout=timeout_sec + 2.0,
            )
        except EdgeBrokerControlError:
            raise
        except asyncio.TimeoutError:
            _raise("edge_broker_timeout")
        except Exception:
            _raise("edge_broker_unavailable")

    async def get_status(self, timeout_sec: float = 3.0) -> BrokerStatus:
        """Read validated diagnostics only; this never requests an endpoint."""
        timeout = _validate_timeout(timeout_sec)
        return _validated_status(await self._request({"op": "status"}, timeout))

    async def ensure_browser(self, timeout_sec: float = 30.0) -> str:
        """Ask the broker to (re)connect to its Edge and wait up to ``timeout_sec``.

        ``get_browser_endpoint`` deliberately sends nothing unless the broker is
        already ready, so a supervisor that must recover a dropped CDP
        connection calls this first.  Returns the broker phase.
        """
        timeout = _validate_timeout(timeout_sec)
        response = await self._request({"op": "ensure_browser", "wait_sec": timeout}, timeout)
        phase = response.get("phase") if isinstance(response, dict) else None
        if phase not in _PHASES:
            _raise("edge_broker_invalid_response")
        return phase

    async def get_browser_endpoint(self, timeout_sec: float = 30.0) -> BrokerBrowserEndpoint:
        """Recheck readiness, then request the broker's existing endpoint.

        No endpoint IPC is sent unless the immediately preceding status response
        confirms supported status IPC, ``phase == 'ready'`` and CDP connected.
        """
        timeout = _validate_timeout(timeout_sec)
        status = await self.get_status(timeout)
        if not status.status_ipc_supported:
            _raise("edge_broker_status_unsupported")
        if status.phase != "ready" or status.cdp_connected is not True:
            if status.phase in {"failed", "reset_required"} and status.error_code:
                _raise(status.error_code)
            _raise("edge_broker_not_ready")

        response = await self._request(
            {"op": "ensure_browser", "wait_sec": timeout},
            timeout,
        )
        if not isinstance(response, dict):
            _raise("edge_broker_invalid_response")
        phase = response.get("phase")
        if phase == "connecting":
            _raise("edge_broker_connecting")
        if phase == "reset_required":
            _raise("edge_broker_reset_required")
        if phase == "failed":
            code = response.get("error")
            if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
                _raise(code)
            _raise("edge_broker_failed")
        if phase != "ready":
            _raise("edge_broker_invalid_response")
        endpoint = _validate_loopback_endpoint(response.get("endpoint"))
        version = response.get("browser_version")
        if not isinstance(version, str) or not _SAFE_VERSION.fullmatch(version.strip()[:200]):
            _raise("edge_broker_invalid_response")
        return BrokerBrowserEndpoint(endpoint)
