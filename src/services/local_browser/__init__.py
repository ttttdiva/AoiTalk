"""Public API for AoiTalk's dedicated local browser runtime."""

from .broker import (
    is_enabled,
    managed_browser_broker_status,
    start_managed_browser_broker,
    stop_managed_browser_broker,
)
from .local_bridge import LocalBrowserBridge, bridge
from .login import LocalBrowserLogin
from .runtime import LocalBrowserRuntime, LocalBrowserUnavailable
from .status import BrowserStatus, LocalBrowserState


def status() -> BrowserStatus:
    """Return the latest in-memory status without performing I/O."""
    return managed_browser_broker_status()


def start() -> bool:
    return start_managed_browser_broker()


def stop() -> None:
    stop_managed_browser_broker()


def login_state(site_url: str) -> dict[str, bool | int | float | None]:
    """Synchronously perform browser I/O and return cookie count/expiry metadata for ``site_url``."""
    return LocalBrowserLogin().login_state(site_url)


__all__ = [
    "BrowserStatus",
    "LocalBrowserBridge",
    "LocalBrowserRuntime",
    "LocalBrowserLogin",
    "LocalBrowserState",
    "LocalBrowserUnavailable",
    "bridge",
    "is_enabled",
    "login_state",
    "start",
    "status",
    "stop",
]
