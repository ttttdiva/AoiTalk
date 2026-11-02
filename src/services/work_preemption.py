"""Which agent work may be stopped at shutdown, and which must not be.

Agent work is *preemptible* by default: at shutdown the AgentWork coordinator
cancels it cooperatively, releases its lease and requeues it without spending
an attempt, so it resumes after the restart.  Its units must therefore be
idempotent and keep their own progress (checkpoint) in the database.

Only the stages that cannot be interrupted safely - an external send, a
partially written external effect, billing, a migration - are wrapped in
``non_preemptible(reason)``.  While such a section is open the work is busy:
shutdown waits for it (briefly) and is refused, unchanged, if it does not end.
A section must persist that its effect happened before it exits, because the
work may be requeued right after it.

A whole kind of work can be non-preemptible by default (``external_action``)
or by ``metadata["preemptible"] = False`` on its WorkItem; the coordinator
then runs the entire adapter inside one section.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Mapping
from typing import Any

# How long a restart waits for open non-preemptible sections before refusing.
NON_PREEMPTIBLE_WAIT_SECONDS = 60.0

# Execution adapters whose whole run is an external effect.
NON_PREEMPTIBLE_ADAPTERS = frozenset({"external_action"})

PREEMPTED_ERROR_CODE = "preempted"

_lock = threading.Lock()
_sections: dict[int, dict[str, Any]] = {}
_next_key = 0

# Set by the coordinator around one adapter run: the claim being executed and
# the coordinator's stop event (a stopping coordinator opens no new section).
_current_claim: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "aoitalk_current_work_claim", default=None
)
_current_stop: contextvars.ContextVar[asyncio.Event | None] = contextvars.ContextVar(
    "aoitalk_current_work_stop", default=None
)


class WorkPreempted(asyncio.CancelledError):
    """Raised instead of opening a section once the coordinator is stopping."""


def claim_is_preemptible(claim: Any) -> bool:
    adapter = str(getattr(claim, "execution_adapter", "") or "")
    metadata = getattr(claim, "metadata", None)
    if adapter in NON_PREEMPTIBLE_ADAPTERS:
        return False
    return not (isinstance(metadata, Mapping) and metadata.get("preemptible") is False)


def bind_claim(claim: Any, stop_event: asyncio.Event | None) -> tuple[Any, Any]:
    """Coordinator hook: mark the current context as running ``claim``."""
    return _current_claim.set(claim), _current_stop.set(stop_event)


def unbind_claim(tokens: tuple[Any, Any]) -> None:
    claim_token, stop_token = tokens
    _current_claim.reset(claim_token)
    _current_stop.reset(stop_token)


class non_preemptible:
    """``with`` / ``async with`` block that shutdown must not interrupt."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "non_preemptible")[:64]
        self._key: int | None = None

    def _open(self) -> None:
        global _next_key
        stop_event = _current_stop.get()
        if stop_event is not None and stop_event.is_set():
            raise WorkPreempted(self.reason)
        claim = _current_claim.get()
        with _lock:
            _next_key += 1
            self._key = _next_key
            _sections[self._key] = {
                "reason": self.reason,
                "work_item_id": getattr(claim, "work_item_id", None),
                "source_type": getattr(claim, "source_type", None),
                "execution_adapter": getattr(claim, "execution_adapter", None),
                "started_monotonic": time.monotonic(),
            }

    def _close(self) -> None:
        with _lock:
            if self._key is not None:
                _sections.pop(self._key, None)
                self._key = None

    def __enter__(self) -> "non_preemptible":
        self._open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._close()

    async def __aenter__(self) -> "non_preemptible":
        self._open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._close()


def active_sections() -> list[dict[str, Any]]:
    """Open non-preemptible sections, oldest first (no internal timing fields)."""
    now = time.monotonic()
    with _lock:
        sections = sorted(_sections.values(), key=lambda item: item["started_monotonic"])
        return [
            {
                **{key: value for key, value in item.items() if key != "started_monotonic"},
                "elapsed_seconds": round(now - item["started_monotonic"], 1),
            }
            for item in sections
        ]


async def wait_for_sections(timeout: float) -> bool:
    """Wait until no section is open; ``False`` if the timeout passed first."""
    deadline = time.monotonic() + max(0.0, timeout)
    while active_sections():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return True
