"""Bridge-shaped target for this server's own PC (browser and desktop).

``resolve_control_bridge`` returns this when no other PC is being controlled.
Browser calls go to the dedicated Edge managed by ``local_browser``; desktop
calls go to the in-process desktop controller.  Both heavy backends stay lazy.
"""

from __future__ import annotations

from typing import Any

LOCAL_DEVICE_ID = "local"


class LocalControlBridge:
    """Same surface as ``RemoteControlBridge`` for the server PC."""

    supports_material_state_guard = False
    media_preparation_backend = None
    hub = None

    def __init__(self, owner: str, *, browser: Any = None, desktop: Any = None) -> None:
        if browser is None:
            from ..local_browser import bridge as local_browser_bridge

            browser = local_browser_bridge()
        if desktop is None:
            from .computer import bridge as local_computer_bridge

            desktop = local_computer_bridge()
        self.owner = owner
        self._browser = browser
        self._desktop = desktop
        self.device_id = LOCAL_DEVICE_ID
        self.browser_backend = browser.browser_backend
        # One lock per backend would let a browser run and a desktop run interleave;
        # the browser's lock is the single task lock, as the Bridge's task_lock was.
        self.operation_lock = browser.operation_lock
        self.connection_epoch = 0

    async def request(self, command: str, **params: Any) -> Any:
        return await self._browser.request(command, **params)

    async def computer(self, action: str, **params: Any) -> Any:
        return await self._desktop.computer(action, **params)
