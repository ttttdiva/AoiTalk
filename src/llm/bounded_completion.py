"""Close a task-owned OpenAI-compatible HTTP request before releasing its worker."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from openai import AsyncOpenAI

from .generation_budget import GenerationBudget


def _announce_send(on_send: Any) -> None:
    """Run the send-evidence callback on the caller's thread, before the private loop exists.

    Its audit write belongs to the caller's loop; run inside the transport's
    short-lived loop it could be cancelled mid-write when that loop is torn down
    (leaving a database lock behind) or lost entirely.
    """

    if callable(on_send):
        try:
            on_send()
        except Exception:
            # Diagnostic callbacks must never alter generation behavior.
            pass


def execute_interruptible_completion(
    client: Any, payload: dict[str, Any], budget: GenerationBudget,
    on_send: Any = None,
) -> Any:
    """Keep absolute deadlines and cancellation active during network waits.

    The caller has already passed the privacy gateway's sender boundary.
    Each request owns its async transport; closing it cannot interrupt a
    sibling request sharing the same local model server. No detached provider
    thread survives this call, and SDK retries cannot escape the task budget.
    """

    async def execute():
        remaining = budget.checkpoint()
        async with AsyncOpenAI(
            base_url=client.base_url,
            api_key=client.api_key,
            organization=client.organization,
            project=client.project,
            timeout=remaining,
            max_retries=0,
        ) as transport:
            budget.checkpoint()
            request = asyncio.create_task(transport.chat.completions.create(**payload))

            async def watch_stop():
                while True:
                    budget.checkpoint()
                    await asyncio.sleep(0.05)

            watchdog = asyncio.create_task(watch_stop())
            try:
                await asyncio.wait((request, watchdog), return_when=asyncio.FIRST_COMPLETED)
                # A completed provider response still owns its usage receipt
                # if cancellation races completion. The caller persists it
                # before its final checkpoint fences the late answer/tools.
                if request.done() and not request.cancelled() and request.exception() is None:
                    return request.result()
                if watchdog.done():
                    watchdog.result()
                return await request
            finally:
                request.cancel()
                watchdog.cancel()
                await asyncio.gather(request, watchdog, return_exceptions=True)

    budget.checkpoint()
    _announce_send(on_send)
    return asyncio.run(execute())


_CLOSE_STEP_SECONDS = 2.0
_logger = logging.getLogger(__name__)


class StreamCleanupIncomplete(RuntimeError):
    """The stream's client-side cleanup could not be shown complete."""


class InterruptibleStream:
    """Iterate a task-owned streaming completion while the budget stays enforced.

    The async transport lives on a private event loop owned by this object, so
    the synchronous consumer can keep its ordinary ``for chunk in stream`` /
    ``close()`` contract.  The budget is checked before every read is
    scheduled, while it waits, and again after its result is retrieved, so
    chunks that are already available cannot outrun a deadline or a cancel.

    Stopping ends the client side of the connection only: whether a remote
    server/GPU stops generating is outside this object's knowledge.  Cleanup is
    never assumed: pending loop tasks are cancelled and awaited, the stream
    and transport are each closed under their own bound, and anything that
    does not finish is reported as ``StreamCleanupIncomplete``.  When work is
    still pending at the bound, the loop and thread are kept and an internal
    reaper owns the eventual cleanup: it waits for that work to end (an
    uncooperative coroutine is never killed, so it can wait indefinitely),
    closes whatever is still open, then stops the loop and ends the thread.
    That also covers a failed construction, which returns no object to retry on.
    A clean close leaves no pending task and no live thread.  A closer that
    raised with no pending work is terminal and unverified: the loop and thread
    are stopped and the failure is reported by every later ``close()``; that
    closer is not run again.  When pending work and a failure are mixed, the
    reaper or a later ``close()`` may retry the closers that did not succeed.
    """

    _END = object()

    def __init__(self, client: Any, payload: dict[str, Any], budget: GenerationBudget, on_send: Any = None) -> None:
        self._budget = budget
        self._closed = False
        self._transport: Any = None
        self._stream: Any = None
        self.cleanup_problems: list[str] = []
        self._closed_resources: set[str] = set()
        self._reaper_started = False
        budget.checkpoint()
        _announce_send(on_send)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="interruptible-stream", daemon=True)
        self._thread.start()
        self._await(lambda: self._open(client, payload))

    def _run_loop(self) -> None:
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _open(self, client: Any, payload: dict[str, Any]) -> None:
        remaining = self._budget.checkpoint()
        self._transport = AsyncOpenAI(
            base_url=client.base_url,
            api_key=client.api_key,
            organization=client.organization,
            project=client.project,
            timeout=remaining,
            max_retries=0,
        )
        self._budget.checkpoint()
        self._stream = await self._transport.chat.completions.create(**payload)

    async def _next(self) -> Any:
        try:
            return await self._stream.__anext__()
        except StopAsyncIteration:
            return self._END

    def _await(self, factory: Any) -> Any:
        try:
            self._budget.checkpoint()  # before anything is scheduled
            future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
            # No separate future.cancel(): shutdown cancels each loop task exactly
            # once and awaits it, so a slow cancellation cleanup is not interrupted twice.
            while not concurrent.futures.wait((future,), timeout=0.05)[0]:
                self._budget.checkpoint()
            result = future.result()
            self._budget.checkpoint()  # a ready result does not bypass a stop
            return result
        except BaseException as exc:
            problems = self._shutdown()
            if problems:
                exc.add_note("stream cleanup incomplete: " + "; ".join(problems))
            raise

    def __iter__(self) -> "InterruptibleStream":
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        chunk = self._await(self._next)
        if chunk is self._END:
            self.close()
            raise StopIteration
        return chunk

    def _shutdown(self) -> list[str]:
        """Cancel and await loop tasks, close stream and transport independently; return problems."""

        if self._closed or not self._thread.is_alive():
            self._closed = True
            return list(self.cleanup_problems)
        if self._reaper_started:
            # The reaper owns cleanup: cancelling it would leave nobody to stop the loop.
            self._thread.join(timeout=_CLOSE_STEP_SECONDS)
            if self._thread.is_alive():
                return ["cleanup still pending (owned by the stream reaper)"]
            self._closed = True
            return list(self.cleanup_problems)
        problems: list[str] = []
        busy: list[bool] = []  # the loop still owns unfinished work: keep it for a retry
        step = _CLOSE_STEP_SECONDS

        async def shutdown() -> None:
            me = asyncio.current_task()

            def pending() -> set[Any]:
                return {task for task in asyncio.all_tasks() if task is not me and not task.done()}

            tasks = pending()
            for task in tasks:
                task.cancel()
            if tasks:
                _, still = await asyncio.wait(tasks, timeout=step)
                if still:
                    busy.append(True)
                    problems.append("request cancellation did not finish")
            for name, resource in (("stream", self._stream), ("transport", self._transport)):
                closer = getattr(resource, "close", None)
                if closer is None or name in self._closed_resources:
                    continue
                closing = asyncio.ensure_future(closer())
                _, hung = await asyncio.wait({closing}, timeout=step)
                if hung:
                    closing.cancel()
                    busy.append(True)
                    problems.append(f"{name} close did not finish")
                elif closing.exception() is not None:
                    problems.append(f"closure failed (terminal, not retried): {name}: {type(closing.exception()).__name__}")
                else:
                    self._closed_resources.add(name)
            if pending():
                busy.append(True)
                problems.append("tasks still pending on the stream loop")

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=step * 4 + 1)
        except BaseException as exc:
            busy.append(True)
            problems.append(f"shutdown did not complete: {type(exc).__name__}")
        if not busy:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=step)
            if self._thread.is_alive():
                problems.append("stream loop thread still alive")
            else:
                self._closed = True
        else:
            self._start_reaper()
        self.cleanup_problems = problems
        if problems:
            _logger.warning("interruptible stream cleanup incomplete: %s", "; ".join(problems))
        return problems

    def _start_reaper(self) -> None:
        """Hand a loop that still owns unfinished work to a cleanup that ends it eventually."""

        if self._reaper_started:
            return
        self._reaper_started = True

        async def reap() -> None:
            me = asyncio.current_task()
            while True:
                pending = {task for task in asyncio.all_tasks() if task is not me and not task.done()}
                if not pending:
                    break
                await asyncio.wait(pending)
            failures: list[str] = []
            for name, resource in (("stream", self._stream), ("transport", self._transport)):
                closer = getattr(resource, "close", None)
                if closer is None or name in self._closed_resources:
                    continue
                try:
                    await closer()
                    self._closed_resources.add(name)
                except Exception as exc:
                    failures.append(f"closure failed (terminal, not retried): {name}: {type(exc).__name__}")
            self.cleanup_problems = failures
            self._closed = True
            self._loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(reap(), self._loop)
        except RuntimeError:
            self._reaper_started = False

    def close(self) -> None:
        problems = self._shutdown()
        if problems:
            raise StreamCleanupIncomplete("; ".join(problems))


def execute_interruptible_stream(
    client: Any, payload: dict[str, Any], budget: GenerationBudget, on_send: Any = None,
) -> InterruptibleStream:
    """Open a streaming completion that the task budget can interrupt."""

    return InterruptibleStream(client, payload, budget, on_send)
