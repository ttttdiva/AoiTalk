"""Run-scoped cooperative cancellation shared by async chat and CLI workers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(eq=False)
class CLILeaseTerminationReceipt:
    """Private, invocation-bound evidence; never serialized into usage/UI.

    A drained Python worker does not prove that its provider process stopped.
    Unknown/custom/scoped process trees deliberately retain their lease.
    """
    reservation_id: Any
    provider: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _workers_started: int = 0
    _workers_completed: int = 0
    _backend_calls: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _processes: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def worker_started(self) -> None:
        with self._lock:
            self._workers_started += 1

    def worker_completed(self) -> None:
        with self._lock:
            self._workers_completed += 1

    def begin_backend_call(self) -> dict[str, Any]:
        ticket = {"covered": False}
        with self._lock:
            self._backend_calls.append(ticket)
        return ticket

    def cover_backend_call(self, ticket: dict[str, Any]) -> None:
        with self._lock:
            if any(item is ticket for item in self._backend_calls):
                ticket["covered"] = True

    def begin_spawn(self) -> dict[str, Any]:
        ticket = {"process": None, "group": None, "windows_job": None, "job_assigned_before_resume": False, "stopped": False}
        with self._lock:
            self._processes.append(ticket)
        return ticket

    def bind_process(self, ticket: dict[str, Any], process: Any, *, isolated_group: bool) -> None:
        with self._lock:
            ticket["process"] = process
            ticket["group"] = process.pid if isolated_group and os.name != "nt" else None

    def no_process_started(self, ticket: dict[str, Any]) -> None:
        with self._lock:
            ticket["stopped"] = True

    def bind_windows_job(self, process: Any, job_handle: int | None, *, assigned_before_resume: bool = False) -> None:
        with self._lock:
            for ticket in self._processes:
                if ticket["process"] is process:
                    ticket["windows_job"] = job_handle
                    ticket["job_assigned_before_resume"] = assigned_before_resume

    def confirm_exit(self, process: Any, *, stopped_windows_job: int | None = None) -> None:
        with self._lock:
            tickets = [item for item in self._processes if item["process"] is process]
        for ticket in tickets:
            # poll() checks the actual owned process. A vanished POSIX session
            # group proves no remaining child; wait/publication/cleanup alone
            # cannot prove it. Windows/scoped groups need their own evidence.
            try:
                if process.poll() is None:
                    continue
            except (OSError, ValueError):
                continue
            if stopped_windows_job and ticket["windows_job"] == stopped_windows_job and ticket["job_assigned_before_resume"]:
                with self._lock:
                    ticket["stopped"] = True
                continue
            if ticket["group"] is None:
                continue
            try:
                os.killpg(ticket["group"], 0)
            except ProcessLookupError:
                with self._lock:
                    ticket["stopped"] = True
            except (OSError, ValueError):
                pass

    def authorizes(self, lease: Any) -> bool:
        with self._lock:
            processes = [item["process"] for item in self._processes if item["process"] is not None and not item["stopped"]]
        for process in processes:
            self.confirm_exit(process)
        with self._lock:
            return (self.reservation_id == lease.reservation_id and self.provider == lease.provider
                and self._workers_started > 0 and self._workers_started == self._workers_completed
                and all(item["covered"] for item in self._backend_calls)
                and all(item["stopped"] for item in self._processes))


_cli_lease_receipt: ContextVar[CLILeaseTerminationReceipt | None] = ContextVar("cli_lease_termination_receipt", default=None)
_cli_backend_ticket: ContextVar[tuple[CLILeaseTerminationReceipt, dict[str, Any]] | None] = ContextVar("cli_backend_call_ticket", default=None)


@contextmanager
def cli_lease_termination_scope(lease: Any):
    receipt = CLILeaseTerminationReceipt(lease.reservation_id, lease.provider)
    token = _cli_lease_receipt.set(receipt)
    try:
        yield receipt
    finally:
        _cli_lease_receipt.reset(token)


def current_cli_lease_receipt() -> CLILeaseTerminationReceipt | None:
    return _cli_lease_receipt.get()


@contextmanager
def cli_backend_call_scope():
    receipt = current_cli_lease_receipt()
    token = _cli_backend_ticket.set((receipt, receipt.begin_backend_call()) if receipt is not None else None)
    try:
        yield
    finally:
        _cli_backend_ticket.reset(token)


def mark_cli_backend_covered() -> None:
    receipt = current_cli_lease_receipt()
    if receipt is not None:
        current = _cli_backend_ticket.get()
        ticket = current[1] if current is not None and current[0] is receipt else receipt.begin_backend_call()
        receipt.cover_backend_call(ticket)


async def run_owned_cli_worker(operation: Callable[[], Any], *, on_cancel: Callable[[], Any] | None = None) -> Any:
    """Drain repeated cancellations; completion is marked by the real thread."""
    handle = get_current_generation_cancellation()
    receipt = current_cli_lease_receipt()
    owned = None
    token = None
    if handle is None and receipt is not None:
        # Direct FreeTeam helpers may have no conversation-run handle. Give
        # this invocation its own cooperative stop event, never a global one.
        owned = register_generation_cancellation(f"cli-lease-{receipt.reservation_id}-{uuid.uuid4()}")
        token = set_current_generation_cancellation(owned)
        handle = owned
    try:
        return await _run_owned_cli_worker(operation, handle=handle, receipt=receipt, on_cancel=on_cancel)
    finally:
        if token is not None:
            reset_current_generation_cancellation(token)
        if owned is not None:
            release_generation_cancellation(owned)


async def _run_owned_cli_worker(operation: Callable[[], Any], *, handle: Any, receipt: CLILeaseTerminationReceipt | None, on_cancel: Callable[[], Any] | None) -> Any:
    # Register queued work immediately so the server cannot mistake an
    # executor-waiting worker for the previous worker's completed result.
    if handle is not None:
        handle.mark_worker_started()
    if receipt is not None:
        receipt.worker_started()
    def run():
        try:
            if handle is not None and handle.cancel_requested.is_set():
                raise GenerationCancelled("CLI generation cancelled before worker invocation")
            return operation()
        finally:
            if handle is not None:
                handle.mark_worker_completed()
            if receipt is not None:
                receipt.worker_completed()
    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if handle is not None:
            handle.request_cancel()
        if on_cancel is not None:
            on_cancel()
        while True:
            try:
                await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                if worker.cancelled():
                    break  # No thread-completion claim is made here.
            except Exception:
                logging.getLogger(__name__).debug("Owned CLI worker drained with an error", exc_info=True)
                break
        raise


class GenerationCancelled(RuntimeError):
    """Raised inside a synchronous generation worker after a stop request."""


class GenerationMutationBlocked(GenerationCancelled):
    """Raised when a late worker reaches a mutation fence after its parent stopped.

    This is a cancellation-derived safety result rather than provider
    generation control flow.  Tool boundaries may convert it into their
    machine-readable failure envelope after rolling back the staged write.
    """


class PlanningInteractionTerminated(RuntimeError):
    """End the active generation after plan approval can no longer continue.

    This is a control-flow signal, not a model-visible tool failure.  It keeps
    the terminal AgentRun state and the safe user-facing reply together while
    deliberately carrying no plan text.
    """

    def __init__(
        self,
        reason: str,
        agent_run_status: str,
        user_message: str,
    ) -> None:
        normalized_reason = (
            str(reason or "planning_interaction_terminated").strip()
            or "planning_interaction_terminated"
        )
        normalized_status = (
            "cancelled"
            if str(agent_run_status).strip().lower() == "cancelled"
            else "failed"
        )
        normalized_message = str(user_message or "").strip()
        self.reason = normalized_reason
        self.agent_run_status = normalized_status
        self.user_message = normalized_message
        super().__init__(normalized_reason)


class AgentDialogueInputPending(PlanningInteractionTerminated):
    """Stop a worker turn after durably recording a human input request.

    Agent dialogue workers cannot wait on the process-local human interaction
    Future used by the root Chat lane.  The service first stores the encrypted
    question and the blocked Run/Work/Input ledger state, then this signal
    ends provider execution.  It intentionally carries only bounded IDs and a
    safe display message; the question body remains in ConversationMessage.
    """

    def __init__(
        self,
        *,
        input_id: str,
        dialogue_id: str | None = None,
        reason: str = "dialogue_input_pending",
        user_message: str = "担当Agentからの質問を受け付けました。回答を待っています。",
    ) -> None:
        self.input_id = str(input_id or "").strip()
        self.dialogue_id = str(dialogue_id or "").strip() or None
        super().__init__(
            reason=reason,
            agent_run_status="failed",
            user_message=user_message,
        )


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
    _cancel_callbacks: list[Callable[[], Any]] = field(default_factory=list, repr=False)
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
            self._cancel_callbacks.clear()

    def request_cancel(self) -> None:
        callbacks: list[Callable[[], Any]]
        with self._interrupt_lock:
            self._accepting_interrupts = False
            self.cancel_requested.set()
            callbacks = list(self._cancel_callbacks)
            self._cancel_callbacks.clear()
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()
                    else:
                        loop.create_task(result)
            except Exception:
                logging.getLogger(__name__).debug(
                    "generation provider cancellation callback failed",
                    exc_info=True,
                )

    def add_cancel_callback(self, callback: Callable[[], Any]) -> Callable[[], None]:
        """Attach one provider-owned stop hook to this exact Run handle.

        The callback is invoked synchronously when a stop wins the Run fence.
        It is intentionally one-shot: after cancellation, a late provider
        callback cannot affect a subsequent continuation or another Run.
        """

        if not callable(callback):
            raise TypeError("generation cancellation callback must be callable")
        invoke_now = False
        with self._interrupt_lock:
            if self.cancel_requested.is_set():
                invoke_now = True
            else:
                self._cancel_callbacks.append(callback)

        if invoke_now:
            self.request_cancel_callback(callback)

        def remove() -> None:
            with self._interrupt_lock:
                self._cancel_callbacks = [
                    item for item in self._cancel_callbacks if item is not callback
                ]

        return remove

    @staticmethod
    def request_cancel_callback(callback: Callable[[], Any]) -> None:
        try:
            result = callback()
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                else:
                    loop.create_task(result)
        except Exception:
            logging.getLogger(__name__).debug(
                "generation provider cancellation callback failed",
                exc_info=True,
            )

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
        raise GenerationMutationBlocked(
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
