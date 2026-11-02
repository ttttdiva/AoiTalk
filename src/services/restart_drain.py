"""Restart reservation (drain): stop admitting new background work, then exit.

While a reservation is active, the shared admission points (AgentWork claims,
GPU index jobs, local agent task submission) refuse *new* work; chat and the
UI keep working.  Once the observed busy state is empty the process runs the
caller-supplied ``shutdown`` (GPU release + graceful exit).  When the deadline
passes first, the reservation is cancelled, the gates reopen and the reason is
kept in ``last_result``.

This module never stops, signals or kills anything by itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Only non-preemptible work is waited for, and it is short.
DEFAULT_TIMEOUT_SECONDS = 60.0
MIN_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 4 * 3600.0
POLL_INTERVAL_SECONDS = 3.0

# Reasons that never hold the drain (kept for older busy collectors).
BACKLOG_REASONS = frozenset({"db_agent_work_claimable"})

CollectBusy = Callable[[], Awaitable[list[str] | None]]
ListWaiting = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
Shutdown = Callable[[str], Awaitable[None]]


class RestartDrainError(Exception):
    """A drain request error carrying only a fixed, safe code."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


class RestartDrain:
    """One in-process restart reservation at a time."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._clock = clock
        self._poll_interval = poll_interval
        self._reset()
        self._last_result: dict[str, Any] | None = None
        # Strong references: the event loop keeps only weak ones to tasks.
        self._watchers: set[asyncio.Task[None]] = set()

    def _reset(self) -> None:
        self._state = "idle"  # idle | draining | shutting_down
        self._operation_id: str | None = None
        self._requested_at: datetime | None = None
        self._deadline: float | None = None
        self._deadline_at: datetime | None = None
        self._waiting_reasons: list[str] = []
        self._waiting_items: list[dict[str, Any]] = []

    def is_draining(self) -> bool:
        return self._state != "idle"

    def status(self) -> dict[str, Any]:
        remaining = None
        if self._deadline is not None:
            remaining = max(0, round(self._deadline - self._clock()))
        return {
            "state": self._state,
            "operation_id": self._operation_id,
            "requested_at": _iso(self._requested_at),
            "deadline_at": _iso(self._deadline_at),
            "remaining_seconds": remaining,
            "waiting_count": len(self._waiting_items),
            "waiting_reasons": list(self._waiting_reasons),
            "waiting_items": [dict(item) for item in self._waiting_items],
            "last_result": dict(self._last_result) if self._last_result else None,
        }

    def reserve(
        self,
        *,
        operation_id: str,
        timeout_seconds: float | None,
        collect_busy: CollectBusy,
        list_waiting: ListWaiting,
        shutdown: Shutdown,
    ) -> dict[str, Any]:
        if self._state != "idle":
            if operation_id == self._operation_id:
                return self.status()
            raise RestartDrainError("restart_pending", 409)
        timeout = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise RestartDrainError("invalid_timeout", 422)
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc)
        self._state = "draining"
        self._operation_id = operation_id
        self._requested_at = now
        self._deadline = self._clock() + timeout
        self._deadline_at = now + timedelta(seconds=timeout)
        watcher = loop.create_task(
            self._run(operation_id, collect_busy, list_waiting, shutdown),
            name=f"restart-drain:{operation_id}",
        )
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        logger.info("Restart drain reserved: operation=%s timeout=%ss", operation_id, timeout)
        return self.status()

    def withdraw(self, reason: str = "operator_cancelled") -> dict[str, Any]:
        if self._state == "idle":
            raise RestartDrainError("not_reserved", 409)
        if self._state == "shutting_down":
            raise RestartDrainError("shutdown_in_progress", 409)
        # The watcher is not interrupted mid-query; it sees the change and returns.
        self._finish("cancelled", reason)
        return self.status()

    def _finish(self, outcome: str, reason: str) -> None:
        self._last_result = {
            "operation_id": self._operation_id,
            "outcome": outcome,
            "reason": reason,
            "finished_at": _iso(datetime.now(timezone.utc)),
            "waiting_reasons": list(self._waiting_reasons),
            "waiting_items": [dict(item) for item in self._waiting_items],
        }
        logger.warning(
            "Restart drain %s (%s): operation=%s waiting=%s",
            outcome, reason, self._operation_id, self._waiting_reasons,
        )
        self._reset()

    def _is_current(self, operation_id: str) -> bool:
        return self._state == "draining" and self._operation_id == operation_id

    @staticmethod
    async def _observe(
        collect_busy: CollectBusy, list_waiting: ListWaiting
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Return the blocking reasons and their description; empty means idle."""
        try:
            reasons = await collect_busy()
        except Exception:
            logger.warning("Restart drain busy observation failed", exc_info=True)
            reasons = None
        if reasons is None:
            return ["busy_state_unknown"], [{"kind": "busy_state_unknown"}]
        blocking = [reason for reason in reasons if reason not in BACKLOG_REASONS]
        if not blocking:
            return [], []
        try:
            return blocking, await list_waiting(blocking)
        except Exception:
            logger.warning("Restart drain waiting list failed", exc_info=True)
            return blocking, [{"kind": reason} for reason in blocking]

    async def _run(
        self,
        operation_id: str,
        collect_busy: CollectBusy,
        list_waiting: ListWaiting,
        shutdown: Shutdown,
    ) -> None:
        while True:
            blocking, items = await self._observe(collect_busy, list_waiting)
            if not self._is_current(operation_id):
                return  # withdrawn while observing
            self._waiting_reasons, self._waiting_items = blocking, items
            if not blocking:
                break
            if self._deadline is not None and self._clock() >= self._deadline:
                self._finish("timed_out", "drain_timeout")
                return
            await asyncio.sleep(self._poll_interval)
            if not self._is_current(operation_id):
                return
        self._state = "shutting_down"
        self._deadline = self._deadline_at = None
        logger.info("Restart drain idle; starting graceful shutdown: operation=%s", operation_id)
        try:
            await shutdown(operation_id)
        except Exception:
            logger.exception("Restart drain shutdown failed")
            self._finish("failed", "shutdown_failed")


restart_drain = RestartDrain()


def is_restart_draining() -> bool:
    """Admission gate for new background work (chat is never gated)."""
    return restart_drain.is_draining()
