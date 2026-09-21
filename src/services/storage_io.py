"""Bounded, per-storage I/O isolation; a stalled mount never owns the ASGI loop.

Threads are daemonized and admission is retained until the actual syscall returns.
Cancelling an await does NOT release its slot and create another blocked worker.
Mutations must check ``check_io_cancelled`` immediately before their commit point.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")
_CANCELLED: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "storage_io_cancelled", default=None
)


@dataclass
class StorageError(Exception):
    code: str
    message: str
    status_code: int = 503
    outcome_unknown: bool = False

    def __str__(self) -> str:
        return self.message

    def detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "outcome_unknown": self.outcome_unknown,
        }


class StorageUnavailable(StorageError):
    def __init__(self, message: str = "ストレージが利用できません", **kwargs):
        super().__init__("storage_unavailable", message, **kwargs)


def check_io_cancelled() -> None:
    cancelled = _CANCELLED.get()
    if cancelled is not None and cancelled.is_set():
        raise StorageUnavailable("ストレージ操作は中断されました")


class StorageIO:
    """No shared executor queue; at most two live workers for a storage key."""
    def __init__(self, per_key: int = 2, maximum: int = 128):
        self.per_key = per_key
        self.maximum = maximum
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    async def run(
        self, key: str, function: Callable[[], T], *, timeout: float = 15.0,
        mutation: bool = False,
    ) -> T:
        with self._lock:
            if self._active.get(key, 0) >= self.per_key or sum(self._active.values()) >= self.maximum:
                raise StorageError("storage_busy", "このストレージの処理待ちです", 503)
            self._active[key] = self._active.get(key, 0) + 1
        result: Future[T] = Future()
        cancelled = threading.Event()
        context = contextvars.copy_context()

        def work() -> None:
            token = _CANCELLED.set(cancelled)
            try:
                check_io_cancelled()
                result.set_result(function())
            except BaseException as exc:
                result.set_exception(exc)
            finally:
                _CANCELLED.reset(token)
                with self._lock:
                    remaining = self._active[key] - 1
                    if remaining:
                        self._active[key] = remaining
                    else:
                        self._active.pop(key, None)

        try:
            threading.Thread(target=lambda: context.run(work), daemon=True,
                             name="aoitalk-storage-io").start()
        except BaseException:
            with self._lock:
                self._active[key] -= 1
                if not self._active[key]:
                    self._active.pop(key)
            raise
        wrapped = asyncio.wrap_future(result)
        # Consume a late error after timeout/cancellation; never cancel the worker.
        wrapped.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
        except asyncio.TimeoutError as exc:
            cancelled.set()
            message = "ストレージが応答しません"
            if mutation:
                message += "。書き込み結果は未確定です。再操作前に状態を確認してください"
            raise StorageUnavailable(message, outcome_unknown=mutation) from exc
        except asyncio.CancelledError:
            cancelled.set()
            raise


storage_io = StorageIO()
