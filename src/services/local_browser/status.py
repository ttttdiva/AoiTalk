"""Thread-safe, value-safe Local Browser status projections."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_BROKER_PHASES = {"idle", "connecting", "ready", "failed", "reset_required", "unavailable", "disabled", "stopped", "unknown"}


@dataclass(frozen=True, slots=True)
class BrowserStatus:
    ready: bool = False
    cdp_connected: bool = False
    mode: str = "dedicated"
    broker_phase: str = "stopped"
    error_code: str | None = None
    restart_count: int = 0
    since_ready_unix: float | None = None

    def __post_init__(self) -> None:
        if type(self.ready) is not bool or type(self.cdp_connected) is not bool:
            raise ValueError("browser_status_boolean_invalid")
        if self.mode != "dedicated":
            raise ValueError("browser_status_mode_invalid")
        if not isinstance(self.broker_phase, str) or self.broker_phase not in _BROKER_PHASES:
            raise ValueError("browser_status_phase_invalid")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not _ERROR_CODE.fullmatch(self.error_code)
        ):
            raise ValueError("browser_status_error_code_invalid")
        if type(self.restart_count) is not int or self.restart_count < 0:
            raise ValueError("browser_status_restart_count_invalid")
        if self.since_ready_unix is not None and (
            type(self.since_ready_unix) not in (int, float) or not math.isfinite(self.since_ready_unix)
        ):
            raise ValueError("browser_status_ready_time_invalid")

    def to_dict(self) -> dict[str, bool | str | int | float | None]:
        """Return the public projection; private endpoint and path data never enter it."""
        return {
            "ready": self.ready,
            "cdp_connected": self.cdp_connected,
            "mode": self.mode,
            "broker_phase": self.broker_phase,
            "error_code": self.error_code,
            "restart_count": self.restart_count,
            "since_ready_unix": self.since_ready_unix,
        }


class LocalBrowserState:
    """Keep the latest immutable status available without doing I/O."""

    def __init__(self, initial: BrowserStatus | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial or BrowserStatus()

    def snapshot(self) -> BrowserStatus:
        with self._lock:
            return self._snapshot

    def publish(self, status: BrowserStatus) -> None:
        if not isinstance(status, BrowserStatus):
            raise TypeError("browser_status_required")
        with self._lock:
            self._snapshot = status
