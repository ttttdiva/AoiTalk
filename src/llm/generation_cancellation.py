"""Run-scoped cooperative cancellation shared by async chat and CLI workers."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


class GenerationCancelled(RuntimeError):
    """Raised inside a synchronous generation worker after a stop request."""


@dataclass
class GenerationMutationGate:
    """Per-delegation fence for writes that outlive an async task cancel.

    ``asyncio.to_thread`` cannot stop a synchronous provider worker.  The
    worker therefore receives this small, thread-safe gate through a
    ``ContextVar`` and checks it immediately before a managed mutation is
    committed.  A new continuation gets a new gate; a late result from the
    cancelled attempt keeps the old gate blocked forever.
    """

    blocked: threading.Event = field(default_factory=threading.Event)
    reason: str = ""

    def block(self, reason: str = "user_interrupt") -> None:
        self.reason = str(reason or "user_interrupt")
        self.blocked.set()

    @property
    def is_blocked(self) -> bool:
        return self.blocked.is_set()


@dataclass
class GenerationInterruptReservation:
    instruction: str
    ready: threading.Event = field(default_factory=threading.Event)
    accepted: bool | None = None
    handle: GenerationCancellationHandle | None = field(default=None, repr=False)

    def resolve(self, accepted: bool) -> None:
        self.accepted = accepted
        self.ready.set()


class GenerationInterrupted(RuntimeError):
    """Raised when the active response must be regenerated with new guidance."""

    def __init__(
        self,
        instructions: list[str] | None = None,
        reservations: list[GenerationInterruptReservation] | None = None,
        continuation_state: object | None = None,
    ) -> None:
        self.instructions = [
            str(instruction).strip()
            for instruction in (instructions or [])
            if str(instruction).strip()
        ]
        self.reservations = list(reservations or [])
        # Agent Team interruptions carry the bounded continuation snapshot out
        # of the generation-attempt ContextVar.  The parent response handler
        # can therefore reset attempt-local state without losing the IDs and
        # pending destination needed to build the retry prompt.
        self.continuation_state = continuation_state
        super().__init__("generation interrupted by steering instruction")

    async def resolve_instructions(self) -> list[str]:
        for reservation in self.reservations:
            await asyncio.to_thread(reservation.ready.wait)
        return [
            *self.instructions,
            *[
                reservation.instruction
                for reservation in self.reservations
                if reservation.accepted is True
            ],
        ]


@dataclass
class GenerationCancellationHandle:
    run_id: str
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    interrupt_requested: threading.Event = field(default_factory=threading.Event)
    worker_started: threading.Event = field(default_factory=threading.Event)
    worker_completed: threading.Event = field(default_factory=threading.Event)
    _worker_lock: threading.Lock = field(default_factory=threading.Lock)
    _interrupt_lock: threading.Lock = field(default_factory=threading.Lock)
    _interrupt_reservations: list[GenerationInterruptReservation] = field(
        default_factory=list
    )
    _active_workers: int = 0
    _accepting_interrupts: bool = True

    def mark_worker_started(self) -> None:
        with self._worker_lock:
            self._active_workers += 1
            self.worker_started.set()
            self.worker_completed.clear()

    def mark_worker_completed(self) -> None:
        with self._worker_lock:
            self._active_workers = max(0, self._active_workers - 1)
            if self._active_workers == 0:
                self.worker_completed.set()

    def reserve_interrupt(
        self, instruction: str
    ) -> GenerationInterruptReservation | None:
        normalized = str(instruction or "").strip()
        if not normalized:
            return None
        with self._interrupt_lock:
            if (
                not self._accepting_interrupts
                or self.cancel_requested.is_set()
            ):
                return None
            reservation = GenerationInterruptReservation(normalized, handle=self)
            self._interrupt_reservations.append(reservation)
            self.interrupt_requested.set()
            return reservation

    def commit_interrupt(self, reservation: GenerationInterruptReservation) -> bool:
        with self._interrupt_lock:
            if reservation.accepted is not None:
                return reservation.accepted
            # A reservation owns the ordering boundary once it has been
            # acquired.  A later final checkpoint or stop may close the
            # handle. A user-requested stop wins, because that task is being
            # cancelled and can no longer apply the steering instruction.
            if self.cancel_requested.is_set():
                reservation.resolve(False)
                return False
            reservation.resolve(True)
            return True

    def abort_interrupt(self, reservation: GenerationInterruptReservation) -> None:
        with self._interrupt_lock:
            if reservation.accepted is None:
                reservation.resolve(False)

    def close_interrupts(self) -> None:
        with self._interrupt_lock:
            self._accepting_interrupts = False

    def request_cancel(self) -> None:
        with self._interrupt_lock:
            self._accepting_interrupts = False
            self.cancel_requested.set()

    def consume_interrupt_instructions(self) -> list[str]:
        with self._interrupt_lock:
            reservations = list(self._interrupt_reservations)
            self._interrupt_reservations.clear()
            self.interrupt_requested.clear()
            return [
                reservation.instruction
                for reservation in reservations
                if reservation.accepted is True
            ]

    def consume_interrupt_reservations(
        self, *, final: bool = False
    ) -> list[GenerationInterruptReservation]:
        with self._interrupt_lock:
            reservations = list(self._interrupt_reservations)
            self._interrupt_reservations.clear()
            self.interrupt_requested.clear()
            if final and not reservations:
                self._accepting_interrupts = False
            return reservations


_handles: dict[str, GenerationCancellationHandle] = {}
_handles_lock = threading.Lock()
_current_handle: ContextVar[GenerationCancellationHandle | None] = ContextVar(
    "aoitalk_generation_cancellation",
    default=None,
)
_current_mutation_gate: ContextVar[GenerationMutationGate | None] = ContextVar(
    "aoitalk_generation_mutation_gate",
    default=None,
)


def register_generation_cancellation(
    run_id: str | None,
) -> GenerationCancellationHandle | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    handle = GenerationCancellationHandle(normalized)
    with _handles_lock:
        _handles[normalized] = handle
    return handle


def request_generation_cancellation(
    run_id: str | None,
) -> GenerationCancellationHandle | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    with _handles_lock:
        handle = _handles.get(normalized)
    if handle is not None:
        handle.request_cancel()
    return handle


def reserve_generation_interrupt(
    run_id: str | None,
    instruction: str,
) -> GenerationInterruptReservation | None:
    """Reserve a steer while its durable user message is being persisted."""
    normalized = str(run_id or "").strip()
    if not normalized or not str(instruction or "").strip():
        return None
    with _handles_lock:
        handle = _handles.get(normalized)
    if handle is None:
        return None
    return handle.reserve_interrupt(instruction)


def request_generation_interrupt(
    run_id: str | None,
    instruction: str,
) -> GenerationInterruptReservation | None:
    """Immediately signal an already-durable/internal steering instruction."""
    reservation = reserve_generation_interrupt(run_id, instruction)
    if reservation is not None:
        commit_generation_interrupt(reservation)
    return reservation


def commit_generation_interrupt(
    reservation: GenerationInterruptReservation | None,
) -> bool:
    if reservation is None:
        return False
    handle = reservation.handle
    return handle.commit_interrupt(reservation) if handle is not None else False


def abort_generation_interrupt(
    reservation: GenerationInterruptReservation | None,
) -> None:
    if reservation is None:
        return
    handle = reservation.handle
    if handle is not None:
        handle.abort_interrupt(reservation)
    elif reservation.accepted is None:
        reservation.resolve(False)


def release_generation_cancellation(
    handle: GenerationCancellationHandle | None,
) -> None:
    if handle is None:
        return
    handle.close_interrupts()
    with _handles_lock:
        if _handles.get(handle.run_id) is handle:
            _handles.pop(handle.run_id, None)


def set_current_generation_cancellation(
    handle: GenerationCancellationHandle | None,
) -> Token:
    return _current_handle.set(handle)


def reset_current_generation_cancellation(token: Token) -> None:
    _current_handle.reset(token)


def get_current_generation_cancellation() -> GenerationCancellationHandle | None:
    return _current_handle.get()


def set_current_generation_mutation_gate(
    gate: GenerationMutationGate | None,
) -> Token:
    """Bind the current delegation's late-result mutation fence."""

    return _current_mutation_gate.set(gate)


def reset_current_generation_mutation_gate(token: Token) -> None:
    _current_mutation_gate.reset(token)


def get_current_generation_mutation_gate() -> GenerationMutationGate | None:
    return _current_mutation_gate.get()


def generation_mutation_blocked() -> bool:
    """Return whether managed writes must be rejected for this attempt."""

    gate = get_current_generation_mutation_gate()
    if gate is not None and gate.is_blocked:
        return True
    handle = get_current_generation_cancellation()
    return bool(
        handle is not None
        and (
            handle.cancel_requested.is_set()
            or handle.interrupt_requested.is_set()
        )
    )


def raise_if_generation_mutation_blocked() -> None:
    """Fail closed before a write when an old attempt was interrupted."""

    if generation_mutation_blocked():
        raise GenerationCancelled(
            "generation mutation blocked after parent interruption"
        )


def raise_if_generation_interrupted(*, final: bool = False) -> None:
    """Raise the pending active-run steering request at a cooperative checkpoint."""
    handle = get_current_generation_cancellation()
    if handle is None:
        return
    # Inspect and consume under the handle's interrupt lock.  The Event is a
    # wake-up hint only; checking it first leaves a race where a final
    # checkpoint can consume a newly reserved steer and still publish the old
    # answer.
    reservations = handle.consume_interrupt_reservations(final=final)
    if reservations:
        raise GenerationInterrupted(reservations=reservations)
