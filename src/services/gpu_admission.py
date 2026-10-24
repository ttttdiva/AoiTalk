"""Cooperative GPU admission shared by the embedding runtime and exclusive trial leases.

One coordinator per GPU backend serializes two classes of work:

* the always-on indexing/embedding path (job tickets for every MediaIndexWorker
  lane, request tickets for every embedding IPC call), and
* an operator-requested exclusive lease (for example a Strata trial) that needs
  the embedding worker unloaded and VRAM headroom free.

Policy, in one paragraph: acquiring a lease first drains *jobs* (DRAIN_INDEX; a
running job may still issue more embedding requests), then drains *runtime*
requests (DRAIN_RUNTIME) and only then crosses the ``begin_release`` barrier.
Before that barrier an expiring or cancelled lease aborts back to OPEN and never
cancels a ticket holder. After the barrier expiry enters proof-gated CLEANUP; OPEN
is reachable only through fresh evidence collected by a trusted in-process
adapter (never supplied by an API caller). Failed proof is a sticky, explicit
ERROR. The ``startup`` RECOVERY phase keeps a freshly booted backend from
assuming an empty GPU.

The coordinator itself performs no I/O, kills nothing and starts nothing; see
``gpu_admission_driver`` (orchestration) and ``gpu_trial_adapter`` (ports).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_BACKEND = "local-gpu"
MAX_LEASE_TTL_SECONDS = 180.0
# Must exceed the slowest legitimate native shutdown (pinned Strata /v1/unload escalates
# QUIT 20s -> terminate 20s -> kill 20s) plus the adapter's own absence waits.
CLEANUP_BUDGET_SECONDS = 420.0
RECOVERY_BUDGET_SECONDS = 420.0
MAX_REPLAY_ENTRIES = 256
PROOF_MAX_AGE_SECONDS = 5.0
WAIT_SLICE_SECONDS = 1.0
_MAX_OWNER_LENGTH = 64
_MAX_OPERATION_LENGTH = 128


class GpuAdmissionConflict(RuntimeError):
    """Stale ownership, a changed replay payload, or an action in the wrong phase."""


Conflict = GpuAdmissionConflict


class GpuAdmissionDeferred(RuntimeError):
    """The gate stayed closed for the whole bounded wait; nothing was submitted."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: float
    profile: str


@dataclass(frozen=True)
class CleanupEvidence:
    """Observation collected by a trusted adapter, bound to a coordinator challenge.

    ``loader_settled`` means the load producer can no longer create unrecorded
    descendants (not merely that a timeout or cancel returned).
    """

    expected: frozenset[ProcessIdentity]
    absent: frozenset[ProcessIdentity]
    loader_settled: bool
    embedding_tree_absent: bool
    capacity_available: bool
    nonce: str
    observed_at: float


def _valid_identity(process) -> bool:
    return (
        isinstance(process, ProcessIdentity)
        and type(process.pid) is int
        and process.pid > 0
        and isinstance(process.created, (int, float))
        and not isinstance(process.created, bool)
        and math.isfinite(process.created)
        and process.created > 0
        and isinstance(process.profile, str)
        and bool(process.profile)
    )


def validate_manifest(manifest) -> frozenset[ProcessIdentity]:
    """Shape check shared by the coordinator and every caller that persists a manifest first."""
    try:
        manifest = frozenset(manifest)
    except TypeError as exc:
        raise ValueError("invalid manifest") from exc
    if not all(_valid_identity(process) for process in manifest):
        raise ValueError("invalid manifest")
    return manifest


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_secret(left: str, right: str) -> bool:
    # compare_digest rejects non-ASCII str; callers may send arbitrary text.
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class GpuAdmissionCoordinator:
    """Thread-safe admission state machine with loop-safe async waiters."""

    def __init__(
        self,
        clock=time.monotonic,
        boot_id: str | None = None,
        max_ttl: float = MAX_LEASE_TTL_SECONDS,
        *,
        recovering: bool = False,
        max_replay: int = MAX_REPLAY_ENTRIES,
        cleanup_budget: float = CLEANUP_BUDGET_SECONDS,
        recovery_budget: float = RECOVERY_BUDGET_SECONDS,
    ):
        self.clock = clock
        self.boot_id = boot_id or uuid.uuid4().hex
        self.max_ttl = float(max_ttl)
        self.max_replay = int(max_replay)
        # CLEANUP / RECOVERY are bounded: past the budget the state is an explicit ERROR
        # (gates stay closed, trusted recovery keeps trying) instead of "in progress" forever.
        self.cleanup_budget = float(cleanup_budget)
        self.recovery_budget = float(recovery_budget)
        self.lock = threading.RLock()
        # Pure policy tests may start OPEN. A production backend with a trial
        # adapter starts in RECOVERY: surviving processes are reconciled first.
        self.phase = "RECOVERY" if recovering else "OPEN"
        self.reason = "startup_recovery" if recovering else None
        self.recovery_pending = recovering  # trial manifest still unknown (RECOVERY or its ERROR)
        self._phase_since = self.clock()
        self.jobs: set[str] = set()
        self.requests: set[str] = set()
        self.spawns: set[str] = set()
        self.lease: dict | None = None
        self.operations: OrderedDict = OrderedDict()
        self.actions: OrderedDict = OrderedDict()
        self.manifest: frozenset[ProcessIdentity] = frozenset()
        self.error: str | None = None
        self.cleanup_requested = False
        self.release_started = False
        self.proof_nonce: str | None = None
        self.proof_since: float | None = None
        self._version = 0
        self._waiters: list = []

    # -- change notification -------------------------------------------------

    @staticmethod
    def _resolve(future) -> None:
        if not future.done():
            future.set_result(None)

    def _changed(self) -> None:
        """Wake async waiters without ever blocking (called with the lock held)."""
        self._version += 1
        waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._resolve, future)
            except RuntimeError:  # the waiter's loop is already closed
                pass

    def _set_phase(self, phase: str) -> None:
        if self.phase != phase:
            self.phase = phase
            self._phase_since = self.clock()
            self._changed()

    def _new_proof(self) -> None:
        self.proof_nonce, self.proof_since = secrets.token_hex(16), self.clock()

    def _open(self) -> None:
        self.lease, self.error, self.reason = None, None, None
        self.manifest = frozenset()
        self.cleanup_requested = self.release_started = False
        self.recovery_pending = False
        self.proof_nonce, self.proof_since = None, None
        self.phase = "OPEN"
        self._phase_since = self.clock()
        self._changed()

    # -- clock-driven transitions -------------------------------------------

    def tick(self) -> str:
        with self.lock:
            if (self.lease and self.clock() >= self.lease["expires"]
                    and self.phase not in ("OPEN", "ERROR") and not self.cleanup_requested):
                self.cleanup_requested, self.reason = True, "lease_expired"
            if self.cleanup_requested and not self.release_started:
                # No external release/trial has started: keep existing work and abort the hold.
                self._open()
            if self.phase == "DRAIN_INDEX" and not self.jobs:
                self._set_phase("DRAIN_RUNTIME")
            if self.cleanup_requested and self.phase not in ("CLEANUP", "ERROR"):
                self._set_phase("CLEANUP")
                self._new_proof()
            # Bounded residual state. This runs on every call and from the watchdog, so it does
            # not depend on any backend call returning; it never opens anything.
            age = self.clock() - self._phase_since
            if self.phase == "CLEANUP" and age >= self.cleanup_budget:
                self.error = "spawn_unsettled" if self.spawns else "cleanup_timeout"
                self._set_phase("ERROR")
                self._new_proof()
            elif self.phase == "RECOVERY" and age >= self.recovery_budget:
                self.error = "recovery_timeout"
                self._set_phase("ERROR")
            return self.phase

    def evidence_context(self) -> tuple[str | None, float]:
        with self.lock:
            self.tick()
            return self.proof_nonce, self.clock()

    def _valid_proof(self, evidence: CleanupEvidence) -> bool:
        return (
            self.proof_nonce is not None
            and isinstance(evidence.nonce, str)
            and _same_secret(evidence.nonce, self.proof_nonce)
            and isinstance(evidence.observed_at, (int, float))
            and math.isfinite(evidence.observed_at)
            and self.proof_since <= evidence.observed_at <= self.clock()
            and self.clock() - evidence.observed_at <= PROOF_MAX_AGE_SECONDS
        )

    # -- lease ownership -----------------------------------------------------

    def _remember(self, store: OrderedDict, key, value) -> None:
        store[key] = value
        keep = self.lease["token"] if self.lease else None
        while len(store) > self.max_replay:
            for old_key, old_value in store.items():
                if keep is None or old_value.get("token") != keep:
                    del store[old_key]
                    break
            else:  # only the live lease remains: nothing evictable
                break

    def _receipt(self, stored: dict) -> dict:
        receipt = dict(stored)
        receipt["active"] = bool(self.lease and _same_secret(self.lease["token"], stored["token"]))
        return receipt

    def acquire(self, owner: str, operation_id: str, ttl) -> dict:
        with self.lock:
            self.tick()
            if (not isinstance(owner, str) or not owner or len(owner) > _MAX_OWNER_LENGTH
                    or not isinstance(operation_id, str) or not operation_id
                    or len(operation_id) > _MAX_OPERATION_LENGTH
                    or isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                    or not 0 < ttl <= self.max_ttl):
                raise ValueError("invalid lease")
            key = (owner, operation_id)
            old = self.operations.get(key)
            if old is not None:
                if old["ttl"] != ttl:
                    raise Conflict("operation payload changed")
                return self._receipt(old)  # Receipt only: callers fetch current status separately.
            if self.phase != "OPEN":
                raise Conflict("resource already reserved")
            self.lease = dict(owner=owner, operation_id=operation_id, ttl=ttl,
                              token=secrets.token_urlsafe(24), boot=self.boot_id,
                              expires=self.clock() + ttl)
            created = dict(self.lease)
            self._remember(self.operations, key, created)
            self.manifest, self.error, self.reason = frozenset(), None, None
            self.cleanup_requested = False
            self._set_phase("DRAIN_INDEX")
            self.tick()
            # The tick may already have expired the lease (tiny TTL, clock jump): the receipt is
            # the immutable record made above, and ``active`` is derived from the state now.
            return self._receipt(created)

    def knows_operation(self, owner: str, operation_id: str) -> bool:
        """Is this a replay of an acquire this coordinator has already answered?"""
        with self.lock:
            return (owner, operation_id) in self.operations

    def _fence(self, owner, token, boot) -> None:
        lease = self.lease
        if (lease is None or not isinstance(token, str) or not isinstance(owner, str)
                or owner != lease["owner"] or boot != self.boot_id
                or not _same_secret(token, lease["token"])):
            raise Conflict("stale ownership")

    def driver_fence(self) -> tuple[str, str, str] | None:
        """Ownership triple for the trusted in-process driver; never serialized or exposed."""
        with self.lock:
            self.tick()
            lease = self.lease
            return (lease["owner"], lease["token"], self.boot_id) if lease else None

    # -- ticket admission ----------------------------------------------------

    def admit_job(self) -> str | None:
        with self.lock:
            self.tick()
            if self.phase != "OPEN":
                return None
            ticket = secrets.token_hex(16)
            self.jobs.add(ticket)
            return ticket  # Caller holds through claim AND finish, even claim=None.

    def finish_job(self, ticket: str) -> None:
        with self.lock:
            if ticket not in self.jobs:
                raise Conflict("unknown job ticket")
            self.jobs.remove(ticket)
            self._changed()
            self.tick()

    def admit_request(self) -> str | None:
        return self.try_admit_request()[0]

    def try_admit_request(self) -> tuple[str | None, int]:
        """Admission attempt plus the version observed in the same critical section."""
        with self.lock:
            self.tick()
            ticket = None
            if self.phase in ("OPEN", "DRAIN_INDEX"):
                ticket = secrets.token_hex(16)
                self.requests.add(ticket)  # Registration atomically precedes executor submission.
            return ticket, self._version

    def finish_request(self, ticket: str) -> None:
        with self.lock:
            if ticket not in self.requests:
                raise Conflict("unknown request ticket")
            self.requests.remove(ticket)
            self._changed()
            self.tick()

    # -- async waiting (never holds the thread lock or blocks the loop) -------

    async def wait_changed(self, version: int, timeout: float) -> int:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = (loop, future)
        with self.lock:
            if self._version != version:
                return self._version
            self._waiters.append(entry)
        try:
            await asyncio.wait_for(future, max(0.0, timeout))
        except asyncio.TimeoutError:
            pass
        finally:
            with self.lock:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    pass
        with self.lock:
            return self._version

    async def admit_request_wait(self, timeout: float) -> str:
        """Bounded wait for a request ticket; cancellation leaves no ticket behind."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            ticket, version = self.try_admit_request()
            if ticket is not None:
                return ticket
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise GpuAdmissionDeferred("gpu_admission_deferred")
            await self.wait_changed(version, min(remaining, WAIT_SLICE_SECONDS))

    # -- release / spawn / cleanup (internal serialized adapter calls) --------

    def begin_release(self, owner, token, boot) -> None:
        with self.lock:
            self.tick()
            self._fence(owner, token, boot)
            if self.phase != "DRAIN_RUNTIME" or self.jobs or self.requests or self.release_started:
                raise Conflict("not ready for release")
            self.release_started = True
            self._new_proof()  # Adapter must perform the formal release AFTER this barrier.
            self._changed()

    def grant(self, owner, token, boot, evidence: CleanupEvidence) -> None:
        with self.lock:
            self.tick()
            self._fence(owner, token, boot)
            if self.phase != "DRAIN_RUNTIME" or self.jobs or self.requests or not self.release_started:
                raise Conflict("not drained")
            if (not self._valid_proof(evidence) or not evidence.loader_settled
                    or not evidence.embedding_tree_absent or not evidence.capacity_available):
                self._new_proof()
                raise Conflict("release not proven")
            self._set_phase("HELD")

    def begin_spawn(self, owner, token, boot) -> str:
        with self.lock:
            self.tick()
            self._fence(owner, token, boot)
            if self.phase != "HELD":
                raise Conflict("not held")
            intent = secrets.token_hex(16)
            self.spawns.add(intent)
            return intent  # MUST precede any external launch; preserved through errors.

    def settle_spawn(self, owner, token, boot, intent, manifest) -> None:
        with self.lock:
            self.tick()
            self._fence(owner, token, boot)
            if intent not in self.spawns:
                raise Conflict("unknown spawn intent")
            # A launch racing expiry must still register its final owned tree.
            manifest = validate_manifest(manifest)
            self.manifest |= manifest
            self.spawns.remove(intent)
            self._new_proof()
            self._changed()

    def begin_cleanup(self, owner, token, boot) -> None:
        with self.lock:
            self._fence(owner, token, boot)
            if not self.cleanup_requested:
                self.reason = "cancelled"
            self.cleanup_requested = True
            self.tick()

    def _settle_cleanup(self, evidence: CleanupEvidence) -> bool:
        if self.phase not in ("CLEANUP", "ERROR") or self.jobs or self.requests or self.spawns:
            raise Conflict("cleanup not drained")
        if self.recovery_pending:
            raise Conflict("recovery not reconciled")  # the surviving manifest is still unknown
        valid = (self._valid_proof(evidence) and evidence.expected == self.manifest
                 and self.manifest <= evidence.absent and evidence.loader_settled
                 and evidence.embedding_tree_absent and evidence.capacity_available)
        if not valid:
            self.error = "cleanup_unproven"
            self._set_phase("ERROR")
            self._new_proof()  # A negative observation invalidates older positive snapshots.
            self._changed()
            return False
        self._open()
        return True

    def finish_cleanup(self, owner, token, boot, evidence: CleanupEvidence) -> bool:
        with self.lock:
            self.tick()
            self._fence(owner, token, boot)
            return self._settle_cleanup(evidence)

    # -- coordinator-owned recovery (watchdog and startup; no owner needed) ---

    def begin_recovery(self, manifest) -> None:
        """Startup reconciliation found these trial-owned processes (possibly none)."""
        with self.lock:
            self.tick()
            # A recovery that overran its budget is an ERROR, but reconciliation may still run.
            if not self.recovery_pending or self.phase not in ("RECOVERY", "ERROR"):
                raise Conflict("not recovering")
            manifest = validate_manifest(manifest)
            self.manifest = manifest
            self.recovery_pending = False
            self.error = None
            self.cleanup_requested = self.release_started = True
            self.reason = "startup_recovery"
            self._set_phase("CLEANUP")
            self._new_proof()

    def recover(self, evidence: CleanupEvidence) -> bool:
        """Proof-gated cleanup completion for a lease whose owner is gone."""
        with self.lock:
            self.tick()
            return self._settle_cleanup(evidence)

    # -- cancellation with caller-held action ids -----------------------------

    def cancel(self, owner, operation_id, token, boot) -> dict:
        """Idempotent owner cancel: same operation id and payload replay the same result."""
        with self.lock:
            self.tick()
            if (not isinstance(operation_id, str) or not operation_id
                    or len(operation_id) > _MAX_OPERATION_LENGTH
                    or not isinstance(token, str) or not isinstance(owner, str)):
                raise ValueError("invalid cancel")
            key = ("cancel", owner, operation_id)
            fingerprint = (_digest(token), boot)
            old = self.actions.get(key)
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("operation payload changed")
                return dict(old["result"])
            self._fence(owner, token, boot)
            self.begin_cleanup(owner, token, boot)
            result = {"cancelled": True, "phase": self.phase, "boot": self.boot_id}
            self._remember(self.actions, key, {"fingerprint": fingerprint, "result": dict(result)})
            return result

    def status(self) -> dict:
        """Observational snapshot: counts and phase only, never tokens or PIDs."""
        with self.lock:
            self.tick()
            lease = self.lease
            return dict(
                phase=self.phase, boot=self.boot_id, jobs=len(self.jobs),
                requests=len(self.requests), spawns=len(self.spawns), error=self.error,
                reason=self.reason, release_started=self.release_started,
                recovery_pending=self.recovery_pending, manifest_size=len(self.manifest),
                owner=lease["owner"] if lease else None,
                expires_in_seconds=max(0.0, lease["expires"] - self.clock()) if lease else None,
            )

    def manifest_snapshot(self) -> frozenset[ProcessIdentity]:
        with self.lock:
            return self.manifest


def fenced_cleanup_targets(manifest, observed):
    """Pure adapter guard. Never return a reused PID or an unknown descendant.

    The caller must revalidate immediately at the OS operation; there is no
    TOCTOU guarantee here. A production Windows adapter should hold process
    handles so the identity cannot change between check and stop.
    """
    by_pid = {process.pid: process for process in observed}
    if any(process.pid in by_pid and by_pid[process.pid] != process for process in manifest):
        raise Conflict("process identity changed")
    return frozenset(process for process in manifest if by_pid.get(process.pid) == process)


_registry: dict[str, GpuAdmissionCoordinator] = {}
_registry_lock = threading.Lock()


def get_gpu_admission(backend: str = DEFAULT_BACKEND) -> GpuAdmissionCoordinator:
    """Shared coordinator for one GPU backend; created OPEN when nothing configured it."""
    with _registry_lock:
        coordinator = _registry.get(backend)
        if coordinator is None:
            coordinator = _registry[backend] = GpuAdmissionCoordinator()
        return coordinator


def configure_gpu_admission(backend: str = DEFAULT_BACKEND, *, recovering: bool = False) -> GpuAdmissionCoordinator:
    """Create the backend coordinator before any ticket holder starts.

    ``recovering`` is only legal while nothing has used the coordinator yet:
    switching an active OPEN coordinator to RECOVERY would orphan its tickets.
    """
    with _registry_lock:
        existing = _registry.get(backend)
        if existing is None:
            existing = _registry[backend] = GpuAdmissionCoordinator(recovering=recovering)
        elif recovering and existing.phase != "RECOVERY":
            raise RuntimeError("GPU admission was used before recovery was configured")
        return existing


def reset_gpu_admission(backend: str | None = None) -> None:
    """Drop coordinators (process shutdown and tests); live tickets are not honoured afterwards."""
    with _registry_lock:
        if backend is None:
            _registry.clear()
        else:
            _registry.pop(backend, None)
