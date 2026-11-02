"""Server-local Windows desktop bridge.

The desktop controller is stateful and initializes COM, so one controller is
created lazily and all of its calls run on this bridge's single worker thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..pc_bridge_store import PcBridgeError

LOCAL_DEVICE_ID = "local"
DESKTOP_SESSION_UNAVAILABLE = "desktop_session_unavailable"
COMPUTER_UNAVAILABLE = "computer_unavailable"


def _desktop_session_unavailable() -> bool:
    """Detect the reliably non-interactive Windows service session (session 0)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        session_id = wintypes.DWORD()
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            return False
        return session_id.value == 0
    except Exception:
        # Session detection is best-effort; DesktopController failures are
        # converted to a fixed availability code below.
        return False


def _create_desktop_controller(*, stop_event: threading.Event) -> Any:
    # Keep pywinauto, PIL, pythoncom, and Win32 imports out of server startup.
    from pc_bridge.desktop import DesktopController

    return DesktopController(stop_event=stop_event)


class LocalComputerBridge:
    """Bridge-shaped access to the server PC's desktop."""

    device_id = LOCAL_DEVICE_ID

    def __init__(
        self,
        *,
        controller_factory: Callable[..., Any] | None = None,
        session_unavailable: Callable[[], bool] | None = None,
    ) -> None:
        self.operation_lock = threading.Lock()
        self.owner: str | None = None
        self._controller_factory = (
            controller_factory if controller_factory is not None else _create_desktop_controller
        )
        self._session_unavailable = (
            session_unavailable if session_unavailable is not None else _desktop_session_unavailable
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-computer")
        self._controller: Any = None

    async def computer(self, action: str, **params: Any) -> Any:
        """Execute one desktop action on the dedicated COM-owning worker."""
        loop = asyncio.get_running_loop()
        stop_event = threading.Event()
        try:
            future = loop.run_in_executor(
                self._executor,
                self._execute,
                action,
                dict(params),
                stop_event,
            )
            # shield keeps the executor future alive so cancellation can signal
            # the running controller and its type/drag loops can stop cleanly.
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        except PcBridgeError as exc:
            if str(exc) == DESKTOP_SESSION_UNAVAILABLE:
                raise
            raise PcBridgeError(COMPUTER_UNAVAILABLE) from None
        except Exception:
            raise PcBridgeError(COMPUTER_UNAVAILABLE) from None

    def _execute(self, action: str, params: dict[str, Any], stop_event: threading.Event) -> Any:
        if stop_event.is_set():
            return None
        try:
            unavailable = self._session_unavailable()
        except Exception:
            unavailable = False
        if unavailable:
            raise PcBridgeError(DESKTOP_SESSION_UNAVAILABLE)

        try:
            controller = self._controller
            if controller is None:
                controller = self._controller_factory(stop_event=stop_event)
                self._controller = controller
            else:
                # Each queued call owns its cancellation event; assigning it
                # only on the worker keeps it aligned with the active action.
                controller.stop_event = stop_event
            return controller.execute(action, params)
        except Exception:
            # Desktop exceptions can contain window titles, paths, or other
            # user data. Never expose or log their messages.
            raise PcBridgeError(COMPUTER_UNAVAILABLE) from None

    def close(self) -> None:
        """Shut down the worker; mainly useful to orderly application teardown."""
        self._executor.shutdown(wait=True, cancel_futures=True)


_bridge: LocalComputerBridge | None = None
_bridge_lock = threading.Lock()


def bridge() -> LocalComputerBridge:
    """Return the process-wide local desktop bridge."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = LocalComputerBridge()
        return _bridge
