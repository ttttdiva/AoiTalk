"""Narrow coordination helper for a graceful operator shutdown.

This module never stops a process, sends a signal, calls ``os._exit``,
restarts anything, or touches the filesystem, environment, network, WSL,
Muse, or a broker.  ``request_exit`` is supplied by the caller and must only
schedule a normal exit on the application's main loop; this helper is
intended to invoke it from a background task after an HTTP response has
already been sent.

This helper does *not* claim a global or exhaustive idle proof.  The real
API must collect the true busy state, and a maintenance window must prevent
concurrent new work.  This class only consumes the explicit busy observation
passed to :meth:`GracefulShutdownGate.prepare`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

ShutdownState = Literal["prepared", "dispatching", "dispatched", "failed"]

_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")


class GracefulShutdownError(Exception):
    """A typed shutdown error containing only a fixed, safe code."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        # Alias for callers that use the more common name.
        self.status_code = http_status


@dataclass(frozen=True, slots=True)
class GracefulShutdownReceipt:
    """Immutable record of the single operation reserved by a gate."""

    boot_id: str
    operation_id: str
    state: ShutdownState

    def to_dict(self) -> dict[str, str]:
        return {
            "boot_id": self.boot_id,
            "operation_id": self.operation_id,
            "state": self.state,
        }

    def as_dict(self) -> dict[str, str]:
        return self.to_dict()


class GracefulShutdownGate:
    """Reserve and dispatch at most one graceful-shutdown operation.

    A successful ``prepare`` only stores a receipt; it never invokes the
    callback.  A successful ``dispatch`` only reports that the caller's
    exit-scheduling callback ran without raising.  It is not proof that the
    process has stopped.
    """

    def __init__(
        self,
        boot_id: str,
        request_exit: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(boot_id, str) or not boot_id.strip():
            raise GracefulShutdownError("invalid_boot_id", 422)
        self._boot_id = boot_id
        self._request_exit = request_exit
        self._lock = threading.Lock()
        self._receipt: GracefulShutdownReceipt | None = None

    def prepare(
        self,
        *,
        expected_boot_id: str,
        operation_id: str,
        busy_reasons: list[str] | tuple[str, ...] | None,
    ) -> GracefulShutdownReceipt:
        """Reserve ``operation_id`` if this boot is explicitly idle.

        Replaying the same boot and operation returns the stored receipt
        without rechecking busy reasons or invoking the callback.  A
        different operation cannot be reserved while one already exists.
        """
        with self._lock:
            # The boot check applies even to an otherwise duplicate request.
            if expected_boot_id != self._boot_id:
                raise GracefulShutdownError("boot_mismatch", 409)

            self._validate_operation_id(operation_id)

            current = self._receipt
            if current is not None:
                if current.operation_id == operation_id:
                    return current
                raise GracefulShutdownError("operation_conflict", 409)

            if not callable(self._request_exit):
                raise GracefulShutdownError("shutdown_unavailable", 503)

            if self._busy_reasons_are_unknown(busy_reasons):
                raise GracefulShutdownError("busy_state_unknown", 503)
            if busy_reasons:
                # Deliberately do not include any supplied reason text.
                raise GracefulShutdownError("operation_busy", 409)

            receipt = GracefulShutdownReceipt(
                boot_id=self._boot_id,
                operation_id=operation_id,
                state="prepared",
            )
            self._receipt = receipt
            return receipt

    def dispatch(self, operation_id: str) -> bool:
        """Invoke the prepared exit callback at most once.

        Returns ``True`` only for the call that successfully invoked the
        callback.  A call that finds the operation already dispatching or
        dispatched returns ``False`` without invoking it again.
        """
        self._validate_operation_id(operation_id)

        with self._lock:
            current = self._receipt
            if current is None or current.operation_id != operation_id:
                raise GracefulShutdownError("operation_not_prepared", 409)
            if current.state in ("dispatching", "dispatched"):
                return False
            if current.state == "failed":
                raise GracefulShutdownError("shutdown_dispatch_failed", 503)
            if not callable(self._request_exit):
                raise GracefulShutdownError("shutdown_unavailable", 503)

            callback = self._request_exit
            # Publish a non-prepared state before releasing the lock and
            # calling user code, so racing dispatches cannot also call it.
            self._receipt = replace(current, state="dispatching")

        try:
            callback()
        except Exception:
            with self._lock:
                self._receipt = replace(current, state="failed")
            # Use a fixed message/code and suppress the callback exception,
            # whose text may contain sensitive implementation details.
            raise GracefulShutdownError(
                "shutdown_dispatch_failed", 503
            ) from None

        with self._lock:
            self._receipt = replace(current, state="dispatched")
        return True

    def snapshot(self) -> GracefulShutdownReceipt | None:
        """Return the current immutable receipt, or ``None`` if unprepared."""
        with self._lock:
            return self._receipt

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if (
            not isinstance(operation_id, str)
            or not 1 <= len(operation_id) <= 128
            or _OPERATION_ID_RE.fullmatch(operation_id) is None
        ):
            raise GracefulShutdownError("invalid_operation_id", 422)

    @staticmethod
    def _busy_reasons_are_unknown(
        busy_reasons: list[str] | tuple[str, ...] | None,
    ) -> bool:
        if busy_reasons is None:
            return True
        # A string, mapping, boolean, set, or other object is not an idle
        # observation: only an explicit list/tuple of strings is accepted.
        if not isinstance(busy_reasons, (list, tuple)):
            return True
        return any(
            not isinstance(reason, str) or not reason.strip()
            for reason in busy_reasons
        )
