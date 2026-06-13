"""Shared Discord bot lifecycle state for API status reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Optional


_lock = RLock()
_state: Dict[str, Any] = {
    "state": "stopped",
    "user": None,
    "user_id": None,
    "guild_count": 0,
    "started_at": None,
    "ready_at": None,
    "stopped_at": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_starting() -> None:
    with _lock:
        _state.update(
            {
                "state": "starting",
                "user": None,
                "user_id": None,
                "guild_count": 0,
                "started_at": _now(),
                "ready_at": None,
                "stopped_at": None,
                "last_error": None,
            }
        )


def mark_ready(*, user: Any, guild_count: int) -> None:
    with _lock:
        _state.update(
            {
                "state": "running",
                "user": str(user) if user is not None else None,
                "user_id": str(getattr(user, "id", "")) if user is not None else None,
                "guild_count": int(guild_count),
                "ready_at": _now(),
                "stopped_at": None,
                "last_error": None,
            }
        )


def mark_stopping() -> None:
    with _lock:
        _state["state"] = "stopping"


def mark_stopped() -> None:
    with _lock:
        _state.update(
            {
                "state": "stopped",
                "stopped_at": _now(),
            }
        )


def mark_failed(error: Any) -> None:
    with _lock:
        _state.update(
            {
                "state": "failed",
                "stopped_at": _now(),
                "last_error": str(error),
            }
        )


def snapshot(*, task_running: Optional[bool] = None) -> Dict[str, Any]:
    with _lock:
        data = dict(_state)
    if task_running is not None:
        data["task_running"] = bool(task_running)
    return data
