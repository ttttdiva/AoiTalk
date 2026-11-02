"""Bounded dispatcher for the existing DOTS dialogue outbox.

This module owns no ledger state.  It commits the service's existing lease and
result transitions in short transactions, then hands one leased item at a time
to the existing SSRF-protected webhook transport.  It deliberately has no
background scheduler; a host may call :meth:`dispatch_batch` on its own
schedule after the bridge has been explicitly enabled.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import math
from typing import Any, Callable

from .dots_dialogue_service import DotsDialogueService


MAX_DISPATCH_BATCH = 25
DEFAULT_DISPATCH_BATCH = 10


class DotsDialogueDispatcherUnavailable(RuntimeError):
    """Raised when the dispatcher has not been explicitly configured."""

    def __init__(self, code: str = "dots_dialogue_dispatcher_unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DispatchBatchResult:
    attempted: int
    outcomes: tuple[str, ...]
    stop_reason: str


class SqlAlchemyDotsDialogueOutboxBackend:
    """Transaction adapter implementing ``DeliveryOutboxBackend``.

    ``session_provider`` may be a SQLAlchemy async sessionmaker or an async
    database-manager method.  No session or secret-bearing delivery is kept
    between calls.
    """

    def __init__(self, *, session_provider: Callable[[], Any], service: DotsDialogueService,
                 principal_fingerprint: str | None = None,
                 dedicated_conversation_id: Any | None = None):
        if not callable(session_provider):
            raise ValueError("session_provider is required")
        if not isinstance(service, DotsDialogueService):
            raise ValueError("dots dialogue service is required")
        self._session_provider = session_provider
        self._service = service
        self._principal_fingerprint = principal_fingerprint
        self._dedicated_conversation_id = dedicated_conversation_id

    async def _in_transaction(self, operation: Callable[[Any], Any]) -> Any:
        session = self._session_provider()
        if inspect.isawaitable(session):
            session = await session
        try:
            result = await operation(session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def lease_next_delivery(self) -> Any:
        return await self._in_transaction(lambda session: self._service.lease_next_delivery(
            session,
            principal_fingerprint=self._principal_fingerprint,
            dedicated_conversation_id=self._dedicated_conversation_id,
        ))

    async def mark_delivery_result(self, outbox_id: str, lease_id: str, http_status: int,
                                   error_code: str | None = None) -> str:
        return await self._in_transaction(lambda session: self._service.mark_delivery_result(
            session, outbox_id=outbox_id, lease_id=lease_id,
            http_status=http_status, error_code=error_code,
        ))


class DotsDialogueOutboxDispatcher:
    """Run a caller-bounded batch using the accepted lease/transport contract."""

    def __init__(self, *, backend: SqlAlchemyDotsDialogueOutboxBackend | None,
                 callback_sender: Any | None, enabled: bool = False):
        self._backend = backend
        self._callback_sender = callback_sender
        self._enabled = enabled is True and backend is not None and callback_sender is not None

    async def dispatch_batch(self, *, max_deliveries: int = DEFAULT_DISPATCH_BATCH) -> DispatchBatchResult:
        if not self._enabled:
            raise DotsDialogueDispatcherUnavailable()
        if (not isinstance(max_deliveries, int) or isinstance(max_deliveries, bool)
                or not 1 <= max_deliveries <= MAX_DISPATCH_BATCH):
            raise ValueError("max_deliveries must be between 1 and 25")

        # Import only when explicitly used.  The transport has no import-time
        # I/O and performs URL/IP/TLS checks at connection time.
        from plugins.AoiTalk_Plugin.aoitalk_plugin.dots_events_transport import dispatch_one_pending

        outcomes: list[str] = []
        for _ in range(max_deliveries):
            outcome = await dispatch_one_pending(self._backend, self._callback_sender)
            if outcome == "idle":
                return DispatchBatchResult(len(outcomes), tuple(outcomes), "idle")
            outcomes.append(outcome)
        return DispatchBatchResult(len(outcomes), tuple(outcomes), "batch_limit")


class DotsDialogueDispatcherLifecycle:
    """Bounded polling lifecycle for the existing outbox dispatcher."""

    def __init__(self, dispatcher: Any | None, *, enabled: bool = False,
                 max_deliveries: int = DEFAULT_DISPATCH_BATCH,
                 interval_seconds: float = 1.0):
        self.dispatcher = dispatcher
        self.max_deliveries = max_deliveries
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self.enabled = bool(
            enabled is True
            and callable(getattr(dispatcher, "dispatch_batch", None))
            and isinstance(max_deliveries, int)
            and not isinstance(max_deliveries, bool)
            and 1 <= max_deliveries <= MAX_DISPATCH_BATCH
            and isinstance(interval_seconds, (int, float))
            and not isinstance(interval_seconds, bool)
            and math.isfinite(float(interval_seconds))
            and float(interval_seconds) > 0
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def dispatch_once(self) -> Any | None:
        if not self.enabled or not callable(getattr(self.dispatcher, "dispatch_batch", None)):
            return None
        try:
            return await self.dispatcher.dispatch_batch(max_deliveries=self.max_deliveries)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Callback URLs, response text, tokens and secrets never reach logs.
            return None

    async def _run(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            await self.dispatch_once()
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(self.interval_seconds))
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="aoitalk-dots-outbox-dispatcher")

    async def stop(self) -> None:
        task = self._task
        stop_event = self._stop_event
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if self._task is task:
                self._task = None
            self._stop_event = None


__all__ = [
    "DEFAULT_DISPATCH_BATCH",
    "MAX_DISPATCH_BATCH",
    "DispatchBatchResult",
    "DotsDialogueDispatcherLifecycle",
    "DotsDialogueDispatcherUnavailable",
    "DotsDialogueOutboxDispatcher",
    "SqlAlchemyDotsDialogueOutboxBackend",
]
