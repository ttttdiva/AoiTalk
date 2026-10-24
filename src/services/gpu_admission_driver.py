"""Watchdog and orchestration for the GPU admission coordinator.

The coordinator is pure policy. This driver is the single in-process owner that
moves a lease through its phases by calling a *trusted backend adapter*:

* DRAIN_RUNTIME: cross the ``begin_release`` barrier, release the embedding
  worker through the existing runtime release, collect fresh evidence, ``grant``.
* CLEANUP / ERROR: stop only the trial-owned manifest, collect fresh evidence,
  and reopen only when the coordinator accepts the proof. Failure stays an
  explicit ERROR and is retried with backoff; it is never turned into OPEN.
* RECOVERY: after a backend restart, reconcile surviving trial processes before
  anything may use the GPU.

The driver, not an API caller, supplies evidence: nothing HTTP-reachable can
claim that a process tree is gone. With no backend configured it is only a
watchdog (``tick``) and leases cannot be granted at all.

Timeouts here are *observations*, never completions. A backend call runs as one
tracked job; the watchdog keeps ticking while it runs, publishes a ``*_timeout``
error once the operation's budget is spent and asks the job to cancel exactly
once. A cancellation-resistant call stays tracked until it really ends: no
second job overlaps it, nothing is released early and nothing is reported as
cancelled. The CLEANUP -> ERROR deadline lives in the coordinator's ``tick`` so
it does not depend on any of this returning.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .gpu_admission import (
    CleanupEvidence,
    Conflict,
    GpuAdmissionCoordinator,
    ProcessIdentity,
    validate_manifest,
)

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_CALL_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
# Operations that legitimately outlast the generic budget. Native shutdown alone can take
# QUIT 20s + terminate 20s + kill 20s inside one unload, followed by the adapter's absence waits.
DEFAULT_BUDGETS = {"cleanup_trial": 300.0, "reconcile_startup": 60.0}


class BackendError(RuntimeError):
    """A backend step failed or could not prove its outcome (message is a short code)."""


class ProducerUnsettled(Conflict):
    """The launch producer may still create descendants; the spawn intent stays open."""


@dataclass(frozen=True)
class LaunchBinding:
    """Broker-verifiable identity of one trial launch: who asked, in which broker incarnation."""

    broker_id: str
    client_id: str
    request_id: str

    def __post_init__(self):
        # An empty or non-text part would compare equal to other empty parts and blur ownership.
        for part in (self.broker_id, self.client_id, self.request_id):
            if not isinstance(part, str) or not part.strip() or len(part) > 256:
                raise ValueError("invalid launch binding")


class GpuLeaseBackend(Protocol):
    """Trusted in-process adapter. Never constructed from, or reachable by, an API caller."""

    async def reconcile_startup(self) -> frozenset[ProcessIdentity]:
        """Trial-owned processes that survived the previous backend (possibly none)."""

    async def release_embedding(self) -> None:
        """Existing idle/busy-safe runtime release; raise BackendError unless unloaded."""

    async def collect_evidence(self, nonce: str, manifest: frozenset[ProcessIdentity], *,
                               clock: Callable[[], float], purpose: str) -> CleanupEvidence:
        """Independent observation after the challenge; ``observed_at`` is taken before probing.

        ``purpose`` is "grant" (headroom the trial needs) or "reopen" (headroom the embedding
        worker needs to be admitted again)."""

    async def cleanup_trial(self, manifest: frozenset[ProcessIdentity]) -> None:
        """Normal exit, then the existing formal stop, for the verified trial-owned tree only."""

    async def note_spawn_intent(self, intent: str, binding: LaunchBinding) -> None:
        """Make the intent and its broker binding durable BEFORE any external launch."""

    async def void_spawn_intent(self, intent: str) -> None:
        """The launch provably never started: close the intent so it cannot block proof."""

    async def note_manifest(self, intent: str, manifest: frozenset[ProcessIdentity]) -> None:
        """Verify ownership, then make the final tree durable BEFORE the coordinator accepts it."""

    async def discover_trial(self, intent: str) -> frozenset[ProcessIdentity]:
        """Verified trial-owned tree of a launch that ended without reporting its tree."""

    async def trial_closed(self) -> None:
        """The coordinator accepted the cleanup proof: forget the persisted trial."""


class _Call:
    """One scope callback invocation: the sole invocation of its operation, with what it carries."""

    __slots__ = ("operation", "task", "payload")

    def __init__(self, operation: str, task: "asyncio.Task", payload=None):
        self.operation, self.task, self.payload = operation, task, payload


class SpawnScope:
    """Brackets one external launch: intent first, tree second, never auto-settled.

    The scope belongs to the *caller's* lease handle (owner, token, boot): a delayed old
    caller can never adopt a newer owner's lease. ``producer_settled`` must return True only
    once the loader/acquisition producer can no longer create unrecorded descendants (the
    thread is done or the broker acknowledged the cancel) - a timeout or coroutine cancel is
    not that - and must stay True once it is. ``settle`` refuses until then, and a scope that
    exits unsettled stays an open intent, so the coordinator cannot reopen; the driver
    settles it once the producer is done.

    **Callback state matrix.** Each of ``note_spawn_intent`` / ``note_manifest`` /
    ``void_spawn_intent`` / ``discover_trial`` is the scope's *sole* invocation of that
    operation and moves through explicit states, never through "no task is left" or "a payload
    is present":

    * NOT_STARTED - ownership kept; it may start exactly once.
    * IN_FLIGHT - the real task is kept in ``_calls`` (also when the awaiting caller is
      cancelled, and also when ``__aenter__`` never returned): no duplicate invocation, the
      coordinator intent stays open, nothing reopens. A call past its budget is asked to cancel
      once and stays tracked.
    * SUCCEEDED - the only state that sets a durable fact (``_intent_recorded``, ``_voided``,
      ``_durable_manifest``, ``_discovered``), once, whether observed by the awaiting caller or
      late by ``_reap_calls``; the matching durable-before-coordinator step may then follow, and
      the write is never repeated.
    * FAILED / CANCELLED - nothing is durable, recovery ownership is kept, and the retry is a
      new attempt that starts only after the old operation really ended.
    """

    def __init__(self, driver: "GpuAdmissionDriver", fence, binding: LaunchBinding,
                 producer_settled: Callable[[], bool]):
        self._driver = driver
        self.fence = fence
        self.binding = binding
        self.producer_settled = producer_settled
        self.intent: str | None = None
        self.settled = False
        self._calls: dict[str, _Call] = {}  # operation -> its sole invocation (IN_FLIGHT only)
        self._cancel_requested: set[str] = set()
        # Durable facts: each is set when - and only when - the matching callback was observed to
        # SUCCEED. The coordinator is settled from these, never from "no call in flight".
        self._intent_recorded = False
        self._voided = False
        self._durable_manifest: frozenset[ProcessIdentity] | None = None
        self._discovered = None  # a successful discover_trial result not yet persisted
        self._never_started = False  # the launch provably did not begin (entry did not complete)

    @property
    def pending_manifest(self) -> frozenset[ProcessIdentity] | None:
        """The manifest carried by a note_manifest call that is still running (not a durable fact)."""
        call = self._calls.get("note_manifest")
        return call.payload if call is not None else None

    def in_flight(self) -> bool:
        return any(not call.task.done() for call in self._calls.values())

    def _apply_success(self, call: _Call, result) -> None:
        operation = call.operation
        if operation == "note_spawn_intent":
            self._intent_recorded = True
        elif operation == "void_spawn_intent":
            self._voided = True
        elif operation == "note_manifest":
            self._durable_manifest = call.payload
        elif operation == "discover_trial":
            self._discovered = result

    def _reap_calls(self) -> None:
        """Consume invocations that ended while nobody was waiting: SUCCEEDED applies its durable
        fact once, FAILED / CANCELLED applies nothing (a retry may then start a new attempt)."""
        for operation, call in list(self._calls.items()):
            task = call.task
            if not task.done():
                continue
            del self._calls[operation]
            self._cancel_requested.discard(operation)
            if task.cancelled() or task.exception() is not None:
                continue
            self._apply_success(call, task.result())

    async def _invoke(self, operation: str, factory: Callable[[], object], payload=None):
        """Run ``factory()`` as the sole invocation of ``operation`` and wait at most its budget.

        An invocation still in flight is awaited instead of being started again. An outcome that
        arrived late is applied first (``_reap_calls``). An overrun is published and raised, never
        mistaken for a result or for completion; a cancelled caller leaves the call tracked."""
        self._reap_calls()
        call = self._calls.get(operation)
        if call is None:
            task = asyncio.ensure_future(factory())
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            call = self._calls[operation] = _Call(operation, task, payload)
        done, _ = await asyncio.wait({call.task}, timeout=self._driver._budget(operation))
        if not done:
            if operation not in self._cancel_requested:
                self._cancel_requested.add(operation)
                call.task.cancel()  # a request only: the task stays tracked until it really ends
            self._driver.driver_error = f"{operation}_timeout"
            raise BackendError(f"{operation}_timeout")
        self._calls.pop(operation, None)
        self._cancel_requested.discard(operation)
        if call.task.cancelled():
            raise BackendError(f"{operation}_cancelled")
        result = call.task.result()  # a FAILED call raises its own error here: nothing is applied
        self._apply_success(call, result)
        return result

    def _register_abandoned(self) -> None:
        if self not in self._driver._abandoned:
            self._driver._abandoned.append(self)

    def _settle_coordinator(self, manifest) -> None:
        """Recovery-side settlement; a lease that already ended has nothing left to register."""
        try:
            self._driver.coordinator.settle_spawn(*self.fence, self.intent, manifest)
        except Conflict:
            pass
        self.settled = True
        if self in self._driver._abandoned:
            self._driver._abandoned.remove(self)  # nothing left to recover

    async def _finish_never_started(self, *, raise_errors: bool) -> None:
        """Close an intent whose launch never began: void it, then settle the coordinator empty.

        Settlement needs a *positively observed* void (``_voided``) and nothing of this scope in
        flight. A failed, cancelled or still-running void leaves the scope registered and the
        intent open, so it can be retried, may reach the bounded ERROR, and never passes as nothing."""
        self._register_abandoned()  # before any await: a failed entry never gets __aexit__
        self._reap_calls()
        if self.in_flight():
            return  # the intent write (or a void) is still running; recovery resumes when it ends
        if not self._voided:
            backend = self._driver.backend
            try:
                await self._invoke("void_spawn_intent", lambda: backend.void_spawn_intent(self.intent))
            except Exception:
                if raise_errors:
                    raise
                log.warning("GPU spawn intent could not be voided", exc_info=True)
                return
        if self.in_flight():
            return
        self._settle_coordinator(())

    async def __aenter__(self) -> "SpawnScope":
        coordinator, backend = self._driver.coordinator, self._driver.backend
        self.intent = coordinator.begin_spawn(*self.fence)
        self._driver._scopes.append(self)  # from here on its callbacks gate any reopen
        try:
            await self._invoke("note_spawn_intent",
                               lambda: backend.note_spawn_intent(self.intent, self.binding))
        except BaseException:
            self._never_started = True  # the launch has not started: the intent is provably empty
            self._register_abandoned()  # first, synchronously: even a cancelled rollback is recovered
            await self._finish_never_started(raise_errors=False)
            raise
        return self

    async def settle(self, manifest) -> None:
        if self.settled:
            raise Conflict("spawn intent already settled")
        if not self.producer_settled():
            raise ProducerUnsettled("producer_unsettled")  # intent retained, nothing persisted
        manifest = validate_manifest(manifest)  # malformed input never reaches durable state
        self._reap_calls()
        current = self._durable_manifest if self._durable_manifest is not None else self.pending_manifest
        if current is not None and manifest != current:
            raise Conflict("a different manifest is already being settled")
        await self._persist_manifest(manifest)
        self._driver.coordinator.settle_spawn(*self.fence, self.intent, manifest)
        self.settled = True

    async def _persist_manifest(self, manifest: frozenset[ProcessIdentity]) -> None:
        """Make ``manifest`` durable through the scope's sole ``note_manifest`` invocation.

        Returns only after the call really SUCCEEDED (``_durable_manifest`` is set by that
        success and by nothing else). A failure leaves nothing durable, so the retry persists it
        afresh *before* the coordinator is ever settled; a call still running is joined, never
        duplicated; an already-durable manifest is never written twice."""
        self._reap_calls()
        if self._durable_manifest == manifest:
            return
        running = self.pending_manifest
        if running is not None and running != manifest:
            raise Conflict("a different manifest is already being persisted")
        backend = self._driver.backend
        await self._invoke("note_manifest", lambda: backend.note_manifest(self.intent, manifest),
                           payload=manifest)

    def ready_to_resume(self) -> bool:
        """May the driver advance this unsettled scope now? Never while a callback is in flight."""
        self._reap_calls()  # a late success must be applied before deciding anything
        if self.in_flight():
            return False
        if self._never_started or self._durable_manifest is not None or self._discovered is not None:
            return True  # a launch to close, a durable manifest to settle, or a tree to persist
        return self._driver._producer_done(self)

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if not self.settled:
            self._register_abandoned()
        return False


class GpuAdmissionDriver:
    def __init__(self, coordinator: GpuAdmissionCoordinator, backend: GpuLeaseBackend | None = None, *,
                 interval: float = DEFAULT_INTERVAL_SECONDS,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
                 budgets: Mapping[str, float] | None = None,
                 retry_delays=DEFAULT_RETRY_DELAYS,
                 step_wait: float | None = None,
                 stop_timeout: float = 2.0):
        self.coordinator = coordinator
        self.backend = backend
        self.interval = interval
        self.call_timeout = call_timeout
        self.budgets = dict(budgets or {})  # explicit overrides win over what the adapter reports
        self.retry_delays = tuple(retry_delays)
        # How long one watchdog step waits on an in-flight job before it ticks again.
        self.step_wait = interval if step_wait is None else step_wait
        self.stop_timeout = stop_timeout
        self._cancel_sent: asyncio.Task | None = None  # the job a cancel was already requested for
        self.driver_error: str | None = None
        self.last_observation: dict | None = None
        self._task: asyncio.Task | None = None
        self._job: asyncio.Task | None = None
        self._job_name: str | None = None
        self._op: tuple[str, float, float] | None = None  # (operation, started, budget)
        self._overdue = False
        self._failures = 0
        self._not_before = 0.0
        self._abandoned: list[SpawnScope] = []
        self._scopes: list[SpawnScope] = []

    @property
    def backend_configured(self) -> bool:
        return self.backend is not None

    @property
    def _lingering(self) -> list[asyncio.Task]:
        """Scope callbacks that ran past their budget or have not been consumed yet."""
        return [call.task for scope in self._scopes for call in scope._calls.values()]

    def callbacks_in_flight(self) -> int:
        """Scope callbacks that can still change durable state. Nothing may reopen while > 0."""
        return sum(1 for scope in self._scopes for call in scope._calls.values() if not call.task.done())

    @property
    def settling(self) -> bool:
        """A backend job or scope callback is still running: no new lease may be granted over it
        (a late durable callback must never land in the next lease's state)."""
        return self._job is not None or self.callbacks_in_flight() > 0

    def _budget(self, operation: str) -> float:
        """Operation-specific: explicit override, else what the adapter's own waits add up to
        (read live, it depends on the recorded launches), else the generic default."""
        if operation in self.budgets:
            return float(self.budgets[operation])
        reported = getattr(self.backend, "budgets", None) or {}
        if operation in reported:
            return float(reported[operation])
        return float(DEFAULT_BUDGETS.get(operation, self.call_timeout))

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="gpu-admission-driver")

    async def stop(self) -> None:
        """Stop the watchdog loop; a backend job still running afterwards stays owned.

        Shutdown is bounded (``stop_timeout``) but never forgets a call that ignored the cancel:
        the driver keeps that very job, so a later ``start``/``step`` re-monitors it (budget,
        reaping, overdue publication) and never launches a second job on top of it."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = self._job
        if job is None:
            return
        if not job.done():
            self._request_cancel(job)
            await asyncio.wait({job}, timeout=self.stop_timeout)
        if job.done():
            self._reap()
        else:
            self.driver_error = f"{self._job_name}_pending_after_stop"
            log.warning("GPU admission backend call %s ignored cancellation at shutdown and stays tracked",
                        self._job_name)

    def _request_cancel(self, job: asyncio.Task) -> None:
        """Ask a job to cancel - once. A resistant call simply stays in flight and tracked."""
        if self._cancel_sent is not job:
            self._cancel_sent = job
            job.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("GPU admission driver step failed")
            await asyncio.sleep(self.interval)

    # -- observation ---------------------------------------------------------

    def snapshot(self) -> dict:
        status = self.coordinator.status()
        op = self._op
        status.update(backend_configured=self.backend_configured, driver_error=self.driver_error,
                      abandoned_spawns=len(self._abandoned), last_observation=self.last_observation,
                      inflight=op[0] if op else None, overdue=self._overdue,
                      callbacks_in_flight=self.callbacks_in_flight())
        return status

    def spawn_scope(self, owner: str, token: str, boot: str, *, binding: LaunchBinding,
                    producer_settled: Callable[[], bool]) -> SpawnScope:
        """Scope for the Strata launch path, bound to the caller's own lease handle.

        Nothing is adopted from the current lease: a stale caller (expired, cancelled, or a
        different owner now holds the GPU) is refused at ``__aenter__`` by the fence."""
        if self.backend is None:
            raise Conflict("gpu_lease_backend_unavailable")
        if not isinstance(binding, LaunchBinding) or not callable(producer_settled):
            raise TypeError("spawn_scope needs a LaunchBinding and a producer_settled callable")
        return SpawnScope(self, (owner, token, boot), binding, producer_settled)

    # -- stepping ------------------------------------------------------------

    def _succeeded(self) -> None:
        self._failures, self._not_before, self.driver_error = 0, 0.0, None

    def _failed(self, code: str) -> None:
        self._failures += 1
        delay = self.retry_delays[min(self._failures, len(self.retry_delays)) - 1]
        self._not_before = asyncio.get_running_loop().time() + delay
        self.driver_error = code

    async def _call(self, operation: str, awaitable):
        """One backend call inside the job; the watchdog times it from outside."""
        self._op = (operation, asyncio.get_running_loop().time(), self._budget(operation))
        self._overdue = False
        try:
            return await awaitable
        finally:
            self._op = None

    def _reap(self) -> None:
        job = self._job
        if job is None or not job.done():
            return
        self._job, self._op, self._overdue, self._cancel_sent = None, None, False, None
        if job.cancelled():
            return
        exc = job.exception()
        if exc is None:
            return
        if isinstance(exc, BackendError):
            self._failed(str(exc))
        else:
            self._failed(type(exc).__name__)
            log.warning("GPU admission backend step failed", exc_info=exc)

    def _check_overdue(self) -> None:
        op = self._op
        if op is None or self._overdue:
            return
        if asyncio.get_running_loop().time() - op[1] > op[2]:
            self._overdue = True
            self._failed(f"{op[0]}_timeout")  # uncertainty is published; it is not a result
            self._request_cancel(self._job)  # once; a resistant call simply stays in flight

    @staticmethod
    def _producer_done(scope: "SpawnScope") -> bool:
        """The caller-supplied check may be buggy; an error means 'not settled', never a crash."""
        try:
            return bool(scope.producer_settled())
        except Exception:
            log.warning("GPU producer_settled check failed", exc_info=True)
            return False

    def _select(self, phase: str):
        coordinator = self.coordinator
        unsettled = False
        for scope in list(self._abandoned):
            if scope.settled:
                self._abandoned.remove(scope)
            elif scope.ready_to_resume():
                return self._settle_abandoned
            else:
                unsettled = True
        status = coordinator.status()
        if phase == "RECOVERY" or (phase == "ERROR" and status["recovery_pending"]):
            return self._recover_startup
        if phase == "DRAIN_RUNTIME":
            return self._release_and_grant
        if phase in ("CLEANUP", "ERROR"):
            if self.callbacks_in_flight():
                # A scope callback can still write durable state: neither stop nor reopen yet.
                # The intent it belongs to is still open, so the coordinator's own deadline turns
                # this into an explicit ERROR even while the callback never returns.
                self.driver_error = "callback_in_flight"
                return None
            if status["spawns"]:
                # A launch may still create descendants: neither stop nor reopen yet.
                self.driver_error = "spawn_unsettled"
                return None
            return self._cleanup
        if self.callbacks_in_flight():
            self.driver_error = "callback_in_flight"
        elif unsettled:
            self.driver_error = "spawn_unsettled"
        elif self.driver_error is not None:
            self._succeeded()
        return None

    async def step(self) -> str:
        coordinator = self.coordinator
        phase = coordinator.tick()  # always first: never gated on a backend call
        if self.backend is None:
            return phase
        # Drop scopes with nothing left to settle; one with a call in flight (or still unsettled) stays.
        self._scopes = [scope for scope in self._scopes
                        if not scope.settled or scope.in_flight() or scope in self._abandoned]
        self._reap()
        if self._job is not None:
            self._check_overdue()
        elif asyncio.get_running_loop().time() >= self._not_before:
            routine = self._select(phase)
            if routine is not None:
                self._job_name = routine.__name__
                self._job = asyncio.create_task(routine(), name=f"gpu-admission-{self._job_name}")
        if self._job is not None and self.step_wait > 0:
            await asyncio.wait({self._job}, timeout=self.step_wait)
            self._reap()
            if self._job is not None:
                self._check_overdue()
        return coordinator.tick()

    # -- routines (each runs as the single in-flight job) --------------------

    async def _settle_abandoned(self) -> None:
        for scope in list(self._abandoned):
            if scope.settled:
                if scope in self._abandoned:
                    self._abandoned.remove(scope)
            elif scope.ready_to_resume():  # never while one of its callbacks is still in flight
                await self._advance_scope(scope)

    async def _advance_scope(self, scope: "SpawnScope") -> None:
        """Carry an unsettled scope forward along the callback state matrix.

        Late outcomes are applied first (``_reap_calls``): a SUCCEEDED call set its durable fact
        once and is never re-issued, a FAILED or CANCELLED one left nothing durable and is retried
        as a new attempt. Only a positively observed success authorizes the next step: a durable
        manifest before the coordinator is told, an observed void before a never-started launch
        is settled. A failure here raises, so the driver backs off and publishes it."""
        scope._reap_calls()
        if scope._never_started:
            await scope._finish_never_started(raise_errors=True)
            return
        if scope._durable_manifest is None:
            if not self._producer_done(scope):
                return
            try:
                if scope._discovered is None:
                    await scope._invoke(
                        "discover_trial", lambda: self.backend.discover_trial(scope.intent))
                manifest = validate_manifest(scope._discovered)
                await scope._persist_manifest(manifest)  # durable BEFORE the coordinator hears of it
            except Exception:
                scope._discovered = None  # observe the tree again before the next attempt
                raise
            scope._discovered = None
        scope._settle_coordinator(scope._durable_manifest)

    async def _recover_startup(self) -> None:
        manifest = await self._call("reconcile_startup", self.backend.reconcile_startup())
        self.coordinator.begin_recovery(manifest)

    async def _release_and_grant(self) -> None:
        coordinator = self.coordinator
        fence = coordinator.driver_fence()
        if fence is None:
            return
        if not coordinator.status()["release_started"]:
            try:
                coordinator.begin_release(*fence)  # barrier BEFORE any external release
            except Conflict:
                return  # runtime tickets are still draining, or ownership just ended
        await self._call("release_embedding", self.backend.release_embedding())
        nonce, _ = coordinator.evidence_context()
        if nonce is None:
            return
        evidence = await self._call("collect_evidence", self.backend.collect_evidence(
            nonce, coordinator.manifest_snapshot(), clock=coordinator.clock, purpose="grant"))
        self._observe(evidence)
        try:
            coordinator.grant(*fence, evidence)
        except Conflict as exc:
            if coordinator.tick() == "DRAIN_RUNTIME":
                raise BackendError("release_not_proven") from exc
            return
        self._succeeded()

    async def _cleanup(self) -> None:
        coordinator = self.coordinator
        if self.callbacks_in_flight():
            raise BackendError("callback_in_flight")
        manifest = coordinator.manifest_snapshot()
        # The gate is closed, so unloading the embedding worker is safe and idempotent; proof
        # requires its tree to be absent even when the original release never succeeded.
        await self._call("release_embedding", self.backend.release_embedding())
        await self._call("cleanup_trial", self.backend.cleanup_trial(manifest))
        nonce, _ = coordinator.evidence_context()
        evidence = await self._call("collect_evidence", self.backend.collect_evidence(
            nonce, coordinator.manifest_snapshot(), clock=coordinator.clock, purpose="reopen"))
        self._observe(evidence)
        if coordinator.manifest_snapshot() != manifest or coordinator.status()["spawns"]:
            return  # a launch registered while we observed: observe the final tree again
        if self.callbacks_in_flight():
            raise BackendError("callback_in_flight")  # last look, immediately before the reopen
        try:
            reopened = coordinator.recover(evidence)
        except Conflict:
            return  # state moved while observing: observe again
        if not reopened:
            raise BackendError("cleanup_unproven")
        self._succeeded()
        try:
            await self._call("trial_closed", self.backend.trial_closed())
        except Exception:
            # The GPU is already proven free; a stale state file only costs a re-verification.
            log.warning("GPU trial state could not be cleared", exc_info=True)

    def _observe(self, evidence: CleanupEvidence) -> None:
        """Booleans only, so an operator can tell *why* proof failed without seeing processes."""
        self.last_observation = dict(
            loader_settled=evidence.loader_settled,
            embedding_tree_absent=evidence.embedding_tree_absent,
            capacity_available=evidence.capacity_available,
            trial_processes_absent=evidence.expected <= evidence.absent,
        )
