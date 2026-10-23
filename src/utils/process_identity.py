"""Visible console titles and startup lines that identify AoiTalk processes.

Only titles of consoles AoiTalk owns are touched: its own console, or the
console a managed runtime child created for itself while the caller has no
console (the detached shared broker).  Nothing here creates a window.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
_ATTACH_LOCK = threading.Lock()
LAUNCH_SOURCE_ENV = "AOITALK_LAUNCH_SOURCE"


def launch_source() -> str:
    return os.environ.get(LAUNCH_SOURCE_ENV, "").strip() or "direct"


def backend_identity() -> str:
    return f"AoiTalk backend | PID {os.getpid()} | launcher: {launch_source()}"


def broker_identity(pid: int | None = None) -> str:
    return f"AoiTalk shared LLM broker | PID {pid or os.getpid()} | started by AoiTalk"


PID_PLACEHOLDER = "{pid}"


def strata_title_template(model: str) -> str:
    """Title with a PID placeholder, filled in once the child exists."""
    return (
        f"AoiTalk Strata | PID {PID_PLACEHOLDER} | model: {model or '-'}"
        f" | started by PID {os.getpid()}"
    )


def set_own_console_title(title: str) -> bool:
    """Retitle the console this process is attached to; no-op without one."""
    if not _WINDOWS:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleWindow():
            return False
        return bool(kernel32.SetConsoleTitleW(title))
    except Exception:
        logger.debug("console title could not be set", exc_info=True)
        return False


def _retitle_child_console(pid: int, title: str, deadline: float) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    while time.monotonic() < deadline:
        with _ATTACH_LOCK:
            # A caller that owns a console must never be detached from it.
            if kernel32.GetConsoleWindow():
                return False
            if kernel32.AttachConsole(pid):
                try:
                    return bool(kernel32.SetConsoleTitleW(title))
                finally:
                    kernel32.FreeConsole()
        time.sleep(0.25)
    return False


def label_child_console_async(pid: int, title: str, *, timeout_seconds: float = 15.0) -> None:
    """Title the auto-created console of a child when this process has none.

    Without a console of its own (the detached broker), Windows gives console
    subsystem children a fresh window titled with their executable path.
    """
    if not _WINDOWS or not pid:
        return

    def run() -> None:
        try:
            _retitle_child_console(pid, title, time.monotonic() + timeout_seconds)
        except Exception:
            logger.debug("child console title failed", exc_info=True)

    threading.Thread(target=run, name=f"console-title-{pid}", daemon=True).start()
