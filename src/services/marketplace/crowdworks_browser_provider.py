"""Resolve the owner-scoped CrowdWorks browser backend."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


LOCAL_DEVICE_ID = "local"
# The browser runs on this server by default; "bridge" (a PC Bridge on another
# PC) is the optional alternative.
DEFAULT_BROWSER_BACKEND = "local"
BROWSER_BACKEND_UNAVAILABLE = "browser_backend_unavailable"
BROWSER_LOGIN_REQUIRED = "browser_login_required"
BROWSER_OWNER_MISMATCH = "browser_owner_mismatch"
SKIPPED_BROWSER_UNAVAILABLE = "skipped_browser_unavailable"
SKIPPED_BROWSER_LOGIN_REQUIRED = "skipped_browser_login_required"

_VALID_LOCAL_ERROR_CODES = frozenset(
    {
        "browser_backend_unavailable",
        "browser_unavailable",
        "browser_restart_limit",
        "broker_executable_missing",
        "broker_mode_legacy",
        "broker_mode_unknown",
        "broker_start_failed",
        "broker_start_timeout",
        "broker_unavailable",
        "cdp_disconnected",
        "gateway_root_missing",
    }
)
_VALID_BRIDGE_ERROR_CODES = frozenset(
    {
        "bridge_browser_command_unavailable",
        "bridge_device_not_found",
        "bridge_device_offline",
        "bridge_disconnected",
        "bridge_jev_runtime_required",
        "bridge_select_a_pc_first",
        "crowdworks_browser_session_unavailable",
    }
)
_MISSING = object()


class BrowserBridgeProvider(Protocol):
    """The shared browser bridge surface used by CrowdWorks consumers."""

    browser_backend: str
    device_id: str
    operation_lock: Any

    async def request(self, command: str, **params: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class BrowserBridgeResolution:
    bridge: BrowserBridgeProvider | None
    ready: bool
    skip_code: str | None
    reason_code: str | None
    backend: str | None
    device_id: str | None


def _configured_backend(config: Any) -> str | None:
    if config is None:
        return DEFAULT_BROWSER_BACKEND

    value = _MISSING
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter("browser.backend", _MISSING)
        except Exception:
            value = _MISSING
    if value is _MISSING:
        try:
            browser = getter("browser", _MISSING) if callable(getter) else _MISSING
        except Exception:
            return None
        if browser is _MISSING and isinstance(config, Mapping):
            browser = config.get("browser", _MISSING)
        if browser is _MISSING:
            return DEFAULT_BROWSER_BACKEND
        if not isinstance(browser, Mapping):
            return None
        value = browser.get("backend", _MISSING)
        if value is _MISSING:
            return DEFAULT_BROWSER_BACKEND
    if isinstance(value, str) and value in {"bridge", "local"}:
        return value
    return None


def _result(
    *,
    backend: str | None,
    device_id: str | None,
    reason_code: str,
) -> BrowserBridgeResolution:
    skip_code = (
        SKIPPED_BROWSER_LOGIN_REQUIRED
        if reason_code == BROWSER_LOGIN_REQUIRED
        else SKIPPED_BROWSER_UNAVAILABLE
    )
    return BrowserBridgeResolution(
        bridge=None,
        ready=False,
        skip_code=skip_code,
        reason_code=reason_code,
        backend=backend,
        device_id=device_id,
    )


def _ready(bridge: BrowserBridgeProvider, *, backend: str, device_id: str) -> BrowserBridgeResolution:
    return BrowserBridgeResolution(
        bridge=bridge,
        ready=True,
        skip_code=None,
        reason_code=None,
        backend=backend,
        device_id=device_id,
    )


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _status_value(status: Any, key: str) -> Any:
    if isinstance(status, Mapping):
        return status.get(key, _MISSING)
    return getattr(status, key, _MISSING)


def _validated_status_error(status: Any) -> str | None:
    value = _status_value(status, "error_code")
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str) and value in _VALID_LOCAL_ERROR_CODES:
        return value
    return None


def _validated_bridge_error(error: Exception) -> str:
    try:
        code = getattr(error, "code", None)
    except Exception:
        code = None
    if isinstance(code, str) and code in _VALID_BRIDGE_ERROR_CODES:
        return code
    # PcBridgeError carries its fixed code as the message. Return it only after
    # an exact allowlist match; arbitrary exception text never reaches callers.
    try:
        message = str(error)
    except Exception:
        return BROWSER_BACKEND_UNAVAILABLE
    if message in _VALID_BRIDGE_ERROR_CODES:
        return message
    return BROWSER_BACKEND_UNAVAILABLE


def _logged_in_value(result: Any) -> bool | None:
    if type(result) is bool:
        return result
    if isinstance(result, Mapping):
        value = result.get("logged_in", _MISSING)
    else:
        value = getattr(result, "logged_in", _MISSING)
    return value if type(value) is bool else None


def _valid_bridge(bridge: Any, *, local: bool) -> bool:
    if not callable(getattr(bridge, "request", None)):
        return False
    backend = getattr(bridge, "browser_backend", None)
    allowed_backends = {"agent_browser"} if local else {"agent_browser", "extension"}
    if backend not in allowed_backends:
        return False
    lock = getattr(bridge, "operation_lock", None)
    if not callable(getattr(lock, "acquire", None)) or not callable(getattr(lock, "release", None)):
        return False
    bridge_device_id = getattr(bridge, "device_id", None)
    return bridge_device_id == LOCAL_DEVICE_ID if local else isinstance(bridge_device_id, str) and bool(bridge_device_id)


async def _resolve_local_browser(owner: str) -> BrowserBridgeResolution:
    try:
        local_browser = importlib.import_module("src.services.local_browser")
        owner_module = importlib.import_module("src.services.local_browser.notify")
        configured_owner_user_id = getattr(owner_module, "configured_owner_user_id", None)
        status_function = getattr(local_browser, "status", None)
        login_state = getattr(local_browser, "login_state", None)
        bridge_function = getattr(local_browser, "bridge", None)
        if (
            not callable(configured_owner_user_id)
            or not callable(status_function)
            or not callable(login_state)
            or not callable(bridge_function)
        ):
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )

        configured_owner = configured_owner_user_id()
        if not isinstance(configured_owner, str) or not configured_owner.strip():
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if configured_owner.strip().casefold() != owner.strip().casefold():
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_OWNER_MISMATCH,
            )

        status = await _await_if_needed(status_function())
        ready = _status_value(status, "ready")
        cdp_connected = _status_value(status, "cdp_connected")
        if type(ready) is not bool or type(cdp_connected) is not bool:
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if not ready or not cdp_connected:
            reason = _validated_status_error(status) or BROWSER_BACKEND_UNAVAILABLE
            return _result(backend="local", device_id=LOCAL_DEVICE_ID, reason_code=reason)

        site_url = "https://crowdworks.jp/"
        if inspect.iscoroutinefunction(login_state):
            login_result = await login_state(site_url)
        else:
            login_result = await asyncio.to_thread(login_state, site_url)
        login_result = await _await_if_needed(login_result)
        logged_in = _logged_in_value(login_result)
        if logged_in is None:
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if not logged_in:
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_LOGIN_REQUIRED,
            )

        bridge = await _await_if_needed(bridge_function())
        if not _valid_bridge(bridge, local=True):
            return _result(
                backend="local",
                device_id=LOCAL_DEVICE_ID,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        return _ready(bridge, backend="local", device_id=LOCAL_DEVICE_ID)
    except Exception:
        return _result(
            backend="local",
            device_id=LOCAL_DEVICE_ID,
            reason_code=BROWSER_BACKEND_UNAVAILABLE,
        )


async def _resolve_bridge(owner: str, device_id: str | None) -> BrowserBridgeResolution:
    selected: str | None = None
    try:
        from ..pc_bridge_service import RemoteControlBridge, get_pc_bridge_hub

        hub = get_pc_bridge_hub()
        selected = await _await_if_needed(hub.store.resolve(owner, device_id, None))
        if not isinstance(selected, str) or not selected:
            return _result(
                backend="bridge",
                device_id=None,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        connection = await _await_if_needed(hub.connection(owner, selected))
        online = getattr(connection, "online", True)
        edge_connected = getattr(connection, "edge_connected", _MISSING)
        capabilities = getattr(connection, "capabilities", _MISSING)
        browser_ready = getattr(connection, "browser_ready", True)
        if type(online) is not bool:
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if not online:
            return _result(backend="bridge", device_id=selected, reason_code="bridge_device_offline")
        if (
            not isinstance(capabilities, (list, tuple, set, frozenset))
            or any(not isinstance(capability, str) for capability in capabilities)
        ):
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if "browser" not in capabilities:
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code="bridge_browser_command_unavailable",
            )
        if type(edge_connected) is not bool or type(browser_ready) is not bool:
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        if not edge_connected:
            return _result(backend="bridge", device_id=selected, reason_code="bridge_disconnected")
        if not browser_ready:
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code="bridge_browser_command_unavailable",
            )
        bridge = RemoteControlBridge(owner, selected, hub)
        if not _valid_bridge(bridge, local=False):
            return _result(
                backend="bridge",
                device_id=selected,
                reason_code=BROWSER_BACKEND_UNAVAILABLE,
            )
        return _ready(bridge, backend="bridge", device_id=selected)
    except Exception as error:
        return _result(
            backend="bridge",
            device_id=selected,
            reason_code=_validated_bridge_error(error),
        )


async def resolve_browser_bridge(
    owner: str,
    *,
    config: Any = None,
    device_id: str | None = None,
) -> BrowserBridgeResolution:
    """Resolve a ready owner-scoped Bridge or local CrowdWorks browser."""
    backend = _configured_backend(config)
    if backend is None or not isinstance(owner, str) or not owner.strip():
        return _result(
            backend=backend,
            device_id=None,
            reason_code=BROWSER_BACKEND_UNAVAILABLE,
        )
    if backend == "local":
        return await _resolve_local_browser(owner)
    return await _resolve_bridge(owner, device_id)


__all__ = [
    "BROWSER_BACKEND_UNAVAILABLE",
    "BROWSER_LOGIN_REQUIRED",
    "BrowserBridgeProvider",
    "BrowserBridgeResolution",
    "SKIPPED_BROWSER_LOGIN_REQUIRED",
    "SKIPPED_BROWSER_UNAVAILABLE",
    "resolve_browser_bridge",
]
