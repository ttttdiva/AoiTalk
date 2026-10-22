"""Drain blocking index work before releasing ownership on cancellation."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import contextvars
import threading


async def run_index_io(operation, *args, lock=None, **kwargs):
    """Retain caller ownership until its executor thread actually finishes.

    Event-loop teardown cancels all Tasks, so a to_thread Task cannot prove
    thread completion. The executing thread signals completion independently.
    """
    finished = threading.Event()

    def invoke():
        try:
            with lock if lock is not None else nullcontext():
                return operation(*args, **kwargs)
        finally:
            finished.set()

    worker = asyncio.get_running_loop().run_in_executor(
        None, contextvars.copy_context().run, invoke
    )

    def consume_exception(future):
        if not future.cancelled():
            future.exception()

    # Cancellation remains the result if blocking work subsequently fails.
    # Consume its error even when completion reaches the loop after the Event.
    worker.add_done_callback(consume_exception)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not finished.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        raise
