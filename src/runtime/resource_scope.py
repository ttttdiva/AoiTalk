"""Ownership helpers for asynchronous resources.

``AsyncResourceScope`` is intentionally a small wrapper around the standard
library's :class:`contextlib.AsyncExitStack`.  It gives callers one owner for
asynchronous context managers, cleanup callbacks, and background tasks while
preserving ``AsyncExitStack``'s LIFO cleanup behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Callable, Coroutine, TypeVar


logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_CleanupCallback = Callable[..., Any]


class AsyncResourceScope:
    """Own asynchronous resources and release them exactly once.

    A scope can be used as an async context manager or closed explicitly with
    :meth:`aclose`.  Once close has begun, registering another resource is an
    error.  Tasks created with :meth:`spawn` are owned by the scope; shutdown
    cancels and awaits each task so task exceptions are always retrieved.
    """

    def __init__(self, name: str | None = None):
        self.name = name
        self._stack = AsyncExitStack()
        self._state_lock = asyncio.Lock()
        self._entering_tasks: set[asyncio.Task[Any]] = set()
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._reported_task_failures: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[bool] | None = None

    def _scope_label(self) -> str:
        return self.name or "unnamed"

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise RuntimeError(
                f"resource scope {self._scope_label()!r} is closing or closed"
            )

    def add_cleanup(
        self,
        callback: _CleanupCallback,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Register a synchronous cleanup callback.

        Callbacks are run by ``AsyncExitStack`` in reverse registration order.
        """

        self._ensure_open()
        self._stack.callback(callback, *args, **kwargs)

    def add_async_cleanup(
        self,
        callback: _CleanupCallback,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Register an asynchronous cleanup callback."""

        self._ensure_open()
        self._stack.push_async_callback(callback, *args, **kwargs)

    async def enter_async_context(self, cm: Any) -> Any:
        """Enter and own an asynchronous context manager.

        The state lock covers the asynchronous enter operation.  This keeps a
        close requested concurrently with setup from closing the exit stack
        before the newly acquired context has been registered.
        """

        current = asyncio.current_task()
        async with self._state_lock:
            self._ensure_open()
            if current is not None:
                self._entering_tasks.add(current)
            try:
                return await self._stack.enter_async_context(cm)
            finally:
                if current is not None:
                    self._entering_tasks.discard(current)

    def spawn(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Create and own a background task.

        A coroutine supplied after shutdown has started is closed before the
        ``RuntimeError`` is raised.  This avoids turning a fail-fast
        registration error into a secondary ``coroutine was never awaited``
        warning.
        """

        try:
            self._ensure_open()
        except RuntimeError:
            self._close_awaitable(coro)
            raise

        try:
            task = asyncio.create_task(coro, name=name)
        except BaseException:
            self._close_awaitable(coro)
            raise
        self._owned_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        try:
            self._stack.push_async_callback(self._cleanup_task, task)
        except BaseException:
            # ``push_async_callback`` is not expected to fail for a live
            # AsyncExitStack, but do not leave a task unowned if it does.
            self._owned_tasks.discard(task)
            if not task.done():
                task.cancel()
            task.add_done_callback(self._consume_task_result)
            raise
        return task

    @staticmethod
    def _close_awaitable(awaitable: Any) -> None:
        close = getattr(awaitable, "close", None)
        if close is None:
            return
        try:
            close()
        except BaseException:
            # The original registration error is the useful failure.  A
            # best-effort close here only prevents an unawaited coroutine.
            return

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._owned_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except BaseException:
            return
        if exception is not None:
            self._report_task_failure(task, exception)

    def _consume_task_result(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            return

    def _report_task_failure(
        self,
        task: asyncio.Task[Any],
        exception: BaseException,
    ) -> None:
        if task in self._reported_task_failures:
            return
        self._reported_task_failures.add(task)
        logger.error(
            "Owned task %s in resource scope %r failed",
            task.get_name(),
            self.name,
            exc_info=(type(exception), exception, exception.__traceback__),
        )

    async def _cleanup_task(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            # Cancellation is the expected result for a running owned task.
            pass
        except Exception as exception:
            # Awaiting retrieves the exception.  The done callback normally
            # reports it first; this path covers a close racing task
            # completion before that callback is scheduled.
            self._report_task_failure(task, exception)

    async def _run_close(
        self,
        exc_details: tuple[Any, Any, Any] | None,
    ) -> bool:
        try:
            # Wait for an in-progress ``enter_async_context`` to finish its
            # registration, then release the lock before running callbacks.
            # Releasing it lets callbacks fail-fast if they try to register a
            # new resource during shutdown.
            async with self._state_lock:
                stack = self._stack
            if exc_details is None:
                await stack.aclose()
                return False
            return bool(await stack.__aexit__(*exc_details))
        finally:
            self._closed = True
            self._owned_tasks.clear()
            self._reported_task_failures.clear()

    @staticmethod
    def _consume_close_result(task: asyncio.Task[bool]) -> None:
        if task.cancelled():
            return
        try:
            # Mark an explicit cleanup exception as retrieved even if every
            # caller waiting on ``aclose`` is itself cancelled.
            task.exception()
        except BaseException:
            return

    async def _close(
        self,
        exc_details: tuple[Any, Any, Any] | None,
    ) -> bool:
        """Run the shared close operation and return suppression status."""

        close_task = self._close_task
        if close_task is None:
            # There is no await before these assignments, so only one caller
            # can win this initialization on the event loop.
            self._closing = True
            close_task = asyncio.create_task(
                self._run_close(exc_details),
                name=f"{self._scope_label()}:close",
            )
            close_task.add_done_callback(self._consume_close_result)
            self._close_task = close_task

        # If a context manager invokes ``aclose`` from inside its own
        # ``__aenter__``, the close task must wait for that enter operation to
        # register its exit callback.  Returning here avoids a self-deadlock;
        # the close task continues as soon as ``enter_async_context`` exits.
        current = asyncio.current_task()
        if current is not None and current in self._entering_tasks:
            return False

        # A cleanup callback may defensively call ``aclose`` on its owner.
        # Awaiting the close task from itself would deadlock; it is already in
        # the middle of the one close operation, so return instead.
        if close_task is asyncio.current_task():
            return False

        # Cancellation of a close waiter must not merely detach the shared
        # close operation.  A lifespan owner may tear down its event loop
        # immediately after ``aclose`` returns, so defer caller cancellation
        # until the owned exit stack has actually drained.
        caller_cancelled = False
        while True:
            try:
                result = await asyncio.shield(close_task)
                break
            except asyncio.CancelledError:
                if close_task.done():
                    # A CancelledError from the shared close task itself is
                    # distinct from cancellation of this waiter.  Retrieve
                    # the result so it is never left unobserved.
                    try:
                        result = close_task.result()
                    except asyncio.CancelledError:
                        raise
                    caller_cancelled = True
                    break

                caller_cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    uncancel = getattr(current, "uncancel", None)
                    if callable(uncancel):
                        uncancel()

        if caller_cancelled:
            raise asyncio.CancelledError
        return result

    async def aclose(self) -> None:
        """Close all owned resources once, safely for concurrent callers."""

        await self._close(None)

    async def __aenter__(self) -> "AsyncResourceScope":
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return await self._close((exc_type, exc, tb))


__all__ = ["AsyncResourceScope"]
