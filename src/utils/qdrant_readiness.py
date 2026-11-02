"""Readiness gate for the managed Qdrant server that starts in the background.

AoiTalk serves FastAPI and Next.js without waiting for Qdrant.  Until the
managed server is ready, Qdrant users get ``QdrantNotReady`` (shown as
"preparing") instead of connection errors.  External or embedded Qdrant, and
processes that never started a managed server, never wait here.
"""

from __future__ import annotations

import asyncio
import threading
import time

# How long a Qdrant user waits for a starting server before reporting "preparing".
WAIT_SECONDS = 5.0

_pending = False
_ready = threading.Event()


class QdrantNotReady(RuntimeError):
    """The managed Qdrant server is still starting or retrying its start."""

    def __init__(self) -> None:
        super().__init__("qdrant_starting: Qdrantを準備中です")


def mark_pending() -> None:
    global _pending
    _ready.clear()
    _pending = True


def mark_ready() -> None:
    _ready.set()


def mark_idle() -> None:
    global _pending
    _pending = False
    _ready.clear()


def starting() -> bool:
    return _pending and not _ready.is_set()


def wait_ready(timeout: float | None = None) -> None:
    """Block (worker threads only) until ready; raise ``QdrantNotReady`` after ``timeout``."""
    if starting() and not _ready.wait(WAIT_SECONDS if timeout is None else timeout) and starting():
        raise QdrantNotReady()


async def await_ready(timeout: float | None = None) -> None:
    deadline = time.monotonic() + (WAIT_SECONDS if timeout is None else timeout)
    while starting():
        if time.monotonic() >= deadline:
            raise QdrantNotReady()
        await asyncio.sleep(0.2)
