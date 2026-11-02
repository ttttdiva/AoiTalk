"""Server-local bridge with the same surface as ``RemoteControlBridge``.

Browser consumers (``BrowserAgentService``, ``BridgeInspectionLease`` and the
CrowdWorks workflow) only need ``await request(command, **params)``,
``browser_backend`` and ``operation_lock``.  This object satisfies that surface
by driving the dedicated Edge profile in-process instead of sending commands to
a PC Bridge over WSS, so the callers do not change.

The dispatch hooks keep the hub's meaning: ``dispatch_preflight`` runs first and
may raise (nothing was sent); ``dispatch_gate`` runs last and, when supplied,
turns a rejection into a blocked, not-executed result.  Run-level exclusivity is
the callers' ``operation_lock``; the backend is not reentrant for concurrent runs.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from urllib.parse import urlsplit

from .runtime import LocalBrowserRuntime, LocalBrowserUnavailable

LOCAL_DEVICE_ID = "local"
BROWSER_UNAVAILABLE = "browser_unavailable"
TARGET_SELECTION_REQUIRED = "agent_browser_target_selection_required"
# Origins the bridge may open a missing tab for. The dedicated profile is the server's own
# browser, but opening arbitrary origins on a model's say-so stays out of scope.
OPEN_IF_MISSING_ORIGINS = frozenset({"https://crowdworks.jp", "https://www.crowdworks.jp"})


def _origin(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


def _general_commands() -> frozenset[str]:
    # Imported lazily: the allowlist lives next to the Bridge protocol and is stdlib-only.
    from pc_bridge.browser_commands import GENERAL_BROWSER_COMMANDS

    return frozenset(GENERAL_BROWSER_COMMANDS)


class LocalBrowserBridge:
    """Dedicated-profile browser exposed through the Bridge-shaped interface."""

    browser_backend = "agent_browser"
    device_id = LOCAL_DEVICE_ID
    # The local backend has no WSS epoch; callers that pin a connection treat it as constant.
    connection_epoch = 0
    supports_material_state_guard = False

    def __init__(self, runtime: LocalBrowserRuntime | None = None) -> None:
        self.runtime = runtime or LocalBrowserRuntime()
        self.operation_lock = threading.Lock()
        self.owner: str | None = None

    async def request(
        self,
        command: str,
        *,
        dispatch_preflight: Any = None,
        dispatch_gate: Any = None,
        **params: Any,
    ) -> dict[str, Any]:
        from pc_bridge.edge import EdgeBridgeError

        if command not in _general_commands():
            raise EdgeBridgeError("bridge_browser_command_unavailable")
        try:
            backend = self.runtime.get_backend()
        except LocalBrowserUnavailable:
            raise EdgeBridgeError(BROWSER_UNAVAILABLE) from None
        try:
            if dispatch_preflight is not None:
                await dispatch_preflight()
            if dispatch_gate is not None:
                dispatch_gate()
        except Exception:
            if dispatch_gate is None:
                raise
            return {
                "ok": False,
                "executed": False,
                "execution_status": "blocked",
                "error": "bridge_dispatch_preflight_rejected",
            }
        try:
            return await backend.request(command, **params)
        except EdgeBridgeError as exc:
            origin = _origin(params.get("url"))
            if command != "resolve_tab" or str(exc) != TARGET_SELECTION_REQUIRED or origin is None:
                raise
        # The Bridge exe picked its tab at setup; here the first resolve_tab does it.
        await asyncio.to_thread(self._select_target, backend, origin)
        return await backend.request(command, **params)

    def _select_target(self, backend: Any, origin: str) -> None:
        """Select the tab whose origin matches, opening one for an allowed origin if missing."""
        from pc_bridge.edge import EdgeBridgeError

        try:
            rows = backend.local_setup("discover_targets", {}).get("targets")
        except EdgeBridgeError as exc:
            if str(exc) != "agent_browser_no_targets":
                raise
            rows = []
        matches = _matching_tabs(rows, origin)
        if not matches and origin in OPEN_IF_MISSING_ORIGINS:
            opened = backend.local_setup("open_target", {"url": origin + "/"})
            matches = _matching_tabs(opened.get("targets") if isinstance(opened, dict) else None, origin)
        if not matches:
            raise EdgeBridgeError("browser_target_not_found")
        # More than one tab of the same origin is normal in the dedicated profile (a
        # stray login tab). Use the oldest handle so repeated runs pick the same tab.
        chosen = min(matches)
        result = backend.local_setup("select_target", {"tab_id": chosen})
        if not isinstance(result, dict) or result.get("selected") is not True:
            raise EdgeBridgeError("browser_target_selection_unverified")


def _matching_tabs(rows: Any, origin: str) -> list[int]:
    if not isinstance(rows, list):
        return []
    return [
        row["tab_id"]
        for row in rows
        if isinstance(row, dict) and row.get("origin") == origin and type(row.get("tab_id")) is int
    ]


_bridge: LocalBrowserBridge | None = None
_bridge_lock = threading.Lock()


def bridge() -> LocalBrowserBridge:
    """Return the process-wide local bridge (one dedicated Edge, one run at a time)."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = LocalBrowserBridge()
        return _bridge
