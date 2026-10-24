"""Trial-ownership logic and ports behind the GPU lease backend.

This is the independent OS/broker boundary the pure coordinator does not have.
Everything here decides *what may be touched* and *what counts as proof*; the
concrete OS, broker and NVML ports are injected, so this module never kills a
process itself and never targets by image name:

* Ownership is bound to the broker, not inferred. A trial launch is recorded
  *before* it starts as (broker incarnation, client id, request id). A runtime
  is the trial's only if the broker's immutable launch provenance (``origin``)
  names that request - or, for a broker that keeps none, a recorded holder
  vouches and no other holder is attached - in the same broker incarnation,
  with the same launch fingerprint, started after the intent. A same-profile
  launch by another caller (even with zero tickets left), a pre-existing
  runtime or a foreign holder is never adopted, asked to exit or stopped. A
  runtime nobody can attribute either way is not touched but blocks proof.
* An unreported launch is observed, made durable, *then* cancelled, and observed
  again (cancelling can drop the very holder that identified it, and a launch
  can race the cancel). It resolves only when the broker reports its acquisition
  terminal; unresolved launches and unattributable runtimes never become an
  empty success.
* Cancelling a producer cancels exactly the recorded acquisition(s), never every
  pending acquisition of the shared profile.
* Manifest identities are verified against that binding *before* they are made
  durable; unverifiable live identities are refused, not stored.
* Stop order: normal exit through the broker's authenticated unload, then the
  existing formal managed stop - both through ``BrokerPort`` - for a runtime
  re-verified (ownership and create_time) immediately beforehand. The shared
  broker job is never closed (``BrokerPort`` has no such operation). Every port
  call has its own budget; an overrunning call is tracked, never repeated on top
  of itself and never treated as an outcome.
* Proof is the independent observation of absence (create_time-fenced), a
  settled loader producer, an absent embedding tree and free headroom. A stop
  count, a parent exit, a busy 409, a lost HTTP reply or a 404 health probe is
  never proof by itself.
* Trial state is persisted before launch/ack so a restarted backend can find a
  surviving trial instead of assuming an empty GPU.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Protocol

from .gpu_admission import CleanupEvidence, ProcessIdentity, validate_manifest
from .gpu_admission_driver import BackendError, LaunchBinding

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("data/gpu_admission/trial-state.json")
EMBEDDING_PROFILE = "embedding"


@dataclass(frozen=True)
class Holder:
    """One acquisition (a ticket, or a still-pending request) attached to a runtime."""

    client_id: str
    request_id: str
    pending: bool = False


@dataclass(frozen=True)
class Origin:
    """The acquisition that *started* a runtime incarnation. The broker must retain it for the
    life of the incarnation: unlike ``holders`` it does not vanish when a ticket is cancelled
    or released, so a launch that raced a cancel can still be attributed afterwards."""

    client_id: str
    request_id: str


@dataclass(frozen=True)
class BrokerRuntime:
    """Read-only view of one managed runtime incarnation (``root`` is None while it is only pending).

    ``holders`` are the acquisitions attached *now*; ``origin`` is the immutable launch
    provenance. ``origin=None`` means the broker cannot say who launched it - such a runtime
    is never adopted by guesswork, and one that could be the trial's blocks cleanup proof."""

    profile: str
    root: ProcessIdentity | None
    launch_fingerprint: str
    broker_id: str
    holders: frozenset[Holder] = frozenset()
    origin: Origin | None = None
    # Where the runtime answers and what it is; only the unload request needs them, never ownership.
    endpoint: str | None = None
    runtime_kind: str = ""


class ProcessPort(Protocol):
    async def live(self, pids: Iterable[int]) -> dict[int, float]:
        """pid -> create_time for the given pids that are running right now (read-only)."""

    async def descendants(self, root: ProcessIdentity) -> frozenset[ProcessIdentity]:
        """Live recursive children of ``root``, stamped with ``root.profile``."""


class BrokerPort(Protocol):
    async def runtimes(self, profile: str) -> list[BrokerRuntime]:
        """Runtimes and pending acquisitions for the profile, with every holder (read-only)."""

    async def cancel_acquisition(self, binding: LaunchBinding) -> None:
        """Cancel exactly this (broker, client, request) acquisition; never any other."""

    async def acquisition_state(self, binding: LaunchBinding) -> str:
        """'pending' (may still start something), 'terminal' (it can no longer start anything,
        and any runtime it started is visible in ``runtimes``) or 'unknown' (no record, e.g. the
        broker restarted). Only 'terminal' resolves an unreported launch."""

    async def request_unload(self, runtime: BrokerRuntime) -> str:
        """Authenticated normal-exit request for exactly this runtime: 'ok' | 'busy' | 'lost'."""

    async def formal_stop(self, runtime: BrokerRuntime) -> int:
        """Existing managed stop for exactly this runtime; the count is not proof."""


class CapacityPort(Protocol):
    async def free_bytes(self) -> int: ...


class EmbeddingPort(Protocol):
    def status(self) -> dict | None: ...

    async def release(self) -> dict: ...


class TrialStateStore(Protocol):
    def load(self) -> "TrialState | None": ...

    def save(self, state: "TrialState") -> None: ...

    def clear(self) -> None: ...


@dataclass(frozen=True)
class Operation:
    binding: LaunchBinding
    at: float  # wall clock of the intent; a process older than this cannot be the launch
    settled: bool = False


@dataclass
class TrialState:
    profile: str
    fingerprint: str
    operations: dict[str, Operation] = field(default_factory=dict)  # spawn intent -> operation
    manifest: frozenset[ProcessIdentity] = frozenset()
    embedding: frozenset[ProcessIdentity] = frozenset()


def _identities(values) -> list[dict]:
    return [{"pid": p.pid, "created": p.created, "profile": p.profile} for p in sorted(values, key=lambda p: p.pid)]


class FileTrialStateStore:
    """Atomic JSON file under data/. A corrupt file is an error, never 'nothing to recover'."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)

    def load(self) -> TrialState | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
            return TrialState(
                profile=str(data["profile"]),
                fingerprint=str(data["fingerprint"]),
                operations={
                    str(intent): Operation(
                        LaunchBinding(str(item["broker_id"]), str(item["client_id"]), str(item["request_id"])),
                        float(item["at"]), bool(item["settled"]))
                    for intent, item in data["operations"].items()},
                manifest=frozenset(ProcessIdentity(int(p["pid"]), float(p["created"]), str(p["profile"]))
                                   for p in data["manifest"]),
                embedding=frozenset(ProcessIdentity(int(p["pid"]), float(p["created"]), str(p["profile"]))
                                    for p in data["embedding"]),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError("trial_state_corrupt") from exc

    def save(self, state: TrialState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps({
            "profile": state.profile, "fingerprint": state.fingerprint,
            "operations": {intent: {"broker_id": op.binding.broker_id, "client_id": op.binding.client_id,
                                    "request_id": op.binding.request_id, "at": op.at, "settled": op.settled}
                           for intent, op in state.operations.items()},
            "manifest": _identities(state.manifest), "embedding": _identities(state.embedding)},
            separators=(",", ":"))
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._retry_sharing(lambda: os.replace(temporary, self.path))

    def clear(self) -> None:
        self._retry_sharing(lambda: self.path.unlink(missing_ok=True))

    @staticmethod
    def _retry_sharing(operation, attempts: int = 8, delay: float = 0.025) -> None:
        """A reader (an operator's editor, a backup scan) briefly holding the file is a Windows
        sharing violation, not a failure of the state: retry a bounded number of times."""
        for attempt in range(attempts):
            try:
                operation()
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)


class RuntimeEmbeddingPort:
    """The shared embedding wrapper, observed without importing or creating it."""

    @staticmethod
    def _runtime():
        module = sys.modules.get("src.rag.embedding")
        return module.peek_embedding_runtime() if module is not None else None

    def status(self) -> dict | None:
        runtime = self._runtime()
        return runtime.get_status() if runtime is not None else None

    async def release(self) -> dict:
        runtime = self._runtime()
        if runtime is None:
            return {"released": False, "reason": "not_loaded", "status": None}
        return await runtime.release()


class CooperativeGpuBackend:
    """``GpuLeaseBackend`` for one trial profile, composed from injected ports."""

    def __init__(self, *, profile: str, fingerprint: str, process: ProcessPort, broker: BrokerPort,
                 capacity: CapacityPort, embedding: EmbeddingPort, store: TrialStateStore,
                 grant_free_bytes: int, reopen_free_bytes: int,
                 cancel_timeout: float = 30.0, unload_timeout: float = 75.0, stop_timeout: float = 60.0,
                 unload_grace: float = 20.0, stop_grace: float = 30.0, poll_interval: float = 0.25,
                 wall_clock: Callable[[], float] = time.time):
        self.profile = profile
        self.fingerprint = fingerprint
        self.process, self.broker, self.capacity = process, broker, capacity
        self.embedding, self.store = embedding, store
        # Headroom the trial needs before it may start, versus what the embedding worker
        # needs to be allowed back: reopening must not demand the trial's (larger) footprint.
        self.required_free_bytes = {"grant": int(grant_free_bytes), "reopen": int(reopen_free_bytes)}
        # The pinned native unload may itself spend QUIT 20s + terminate 20s + kill 20s, so the
        # reply budget exceeds that; each wait is bounded separately instead of one short cap.
        self.cancel_timeout, self.unload_timeout, self.stop_timeout = cancel_timeout, unload_timeout, stop_timeout
        self.unload_grace, self.stop_grace = unload_grace, stop_grace
        self.poll_interval = poll_interval
        self.wall_clock = wall_clock
        self._state: TrialState | None = None
        self._closed = False
        # One worker thread does every store read, write and clear: they can never overlap (the
        # file is one ``.tmp`` + replace), whatever happens to the coroutines awaiting them.
        self._store_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-trial-state")
        self._inflight: dict[tuple, asyncio.Task] = {}
        self.embedding_tree: frozenset[ProcessIdentity] = frozenset()
        # Why runtimes were deliberately left alone (bounded: cleanup is retried indefinitely).
        self.skipped: deque[str] = deque(maxlen=32)

    @property
    def budgets(self) -> dict[str, float]:
        """What the driver should allow each operation, derived from this adapter's own waits."""
        recorded = max(1, len(self._state.operations) if self._state else 1)
        cleanup = (self.cancel_timeout * recorded + self.unload_timeout + self.unload_grace
                   + self.stop_timeout + self.stop_grace + 30.0)
        return {"cleanup_trial": cleanup, "reconcile_startup": self.cancel_timeout * recorded + 60.0}

    # -- durable trial state (file I/O stays off the event loop) ----------------

    async def _store_io(self, function, *args):
        """Run one store operation on the single store thread, in submission order.

        The slot is the *thread*, not a coroutine lock: a caller that is cancelled (once or
        repeatedly) stops waiting but neither abandons the operation nor frees the slot, so the
        next read/write/clear queues behind it instead of overlapping a thread that is still
        inside it. The operation is never cancelled (it may already be mid-write)."""
        if self._closed:
            raise BackendError("store_closed")
        try:
            submitted = self._store_executor.submit(function, *args)
        except RuntimeError as exc:  # the executor was shut down underneath us
            raise BackendError("store_closed") from exc
        wrapped = asyncio.wrap_future(submitted)
        wrapped.add_done_callback(lambda f: f.cancelled() or f.exception())  # consumed even if nobody waits
        return await asyncio.shield(wrapped)

    async def _current(self) -> TrialState:
        if self._state is None:
            loaded = await self._store_io(self.store.load)
            if self._state is None:
                self._state = loaded or TrialState(self.profile, self.fingerprint)
        return self._state

    async def _persist(self) -> None:
        state = self._state
        # The snapshot is taken now, in call order, and queued behind any earlier store operation.
        snapshot = TrialState(state.profile, state.fingerprint, dict(state.operations),
                              frozenset(state.manifest), self.embedding_tree)
        await self._store_io(self.store.save, snapshot)

    def bind_fingerprint(self, fingerprint: str) -> None:
        """The launch build the next trial will run (the broker's own ``validate`` answer).

        Ownership is judged against it, so an empty one is refused rather than matching nothing
        silently, and a recorded trial of another build is never re-labelled."""
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise BackendError("trial_fingerprint_unbound")
        state = self._state
        if state is not None and (state.operations or state.manifest) and state.fingerprint not in ("", fingerprint):
            raise BackendError("trial_fingerprint_changed")
        self.fingerprint = fingerprint
        if state is not None:
            state.fingerprint = fingerprint

    def recorded_bindings(self) -> list[LaunchBinding]:
        """Every launch binding of the recorded trial: the only acquisitions this backend may
        recognise (a port maps the broker's opaque digests back to these, and to nothing else)."""
        state = self._state
        return [operation.binding for operation in state.operations.values()] if state is not None else []

    async def note_spawn_intent(self, intent: str, binding: LaunchBinding) -> None:
        if not self.fingerprint:
            raise BackendError("trial_fingerprint_unbound")  # nothing could ever be attributed to the launch
        state = await self._current()
        if state.fingerprint != self.fingerprint:
            if state.operations or state.manifest:
                raise BackendError("trial_fingerprint_changed")
            state.fingerprint = self.fingerprint
        state.operations[intent] = Operation(binding, self.wall_clock())
        await self._persist()

    async def void_spawn_intent(self, intent: str) -> None:
        """The launch provably never started (its intent could not be recorded, or it was refused)."""
        state = await self._current()
        operation = state.operations.get(intent)
        if operation is not None and not operation.settled:
            state.operations[intent] = replace(operation, settled=True)
            await self._persist()

    async def note_manifest(self, intent: str, manifest) -> None:
        state = await self._current()
        operation = state.operations.get(intent)
        if operation is None:
            raise BackendError("unknown_spawn_intent")
        manifest = validate_manifest(manifest)
        if any(process.profile != self.profile for process in manifest):
            raise ValueError("manifest_wrong_profile")
        await self._verify_ownership(manifest, state)  # nothing is persisted if this refuses
        state.manifest = frozenset(state.manifest | manifest)
        state.operations[intent] = replace(operation, settled=True)
        await self._persist()

    async def trial_closed(self) -> None:
        """Cleanup was accepted: forget the persisted trial (a stale file only re-verifies)."""
        self._state, self.embedding_tree = None, frozenset()
        # A port call still running keeps its slot: forgetting it would let the next trial issue
        # the same operation on top of it. Only finished calls are dropped.
        self._inflight = {slot: task for slot, task in self._inflight.items() if not task.done()}
        await self._store_io(self.store.clear)  # queued behind any write still on the store thread

    async def aclose(self) -> None:
        """Server shutdown: take no new store work, never discard queued or running writes.

        ``shutdown(wait=True)`` lets the store thread finish every write already submitted (the
        state file is the only thing that lets a restarted backend find a surviving trial), so the
        executor is stopped from a worker thread and awaited. Port calls still running only get a
        cancel request; they stay owned by the broker/OS, which this backend never closes."""
        self._closed = True
        for task in list(self._inflight.values()):
            if not task.done():
                task.cancel()
        await asyncio.to_thread(self._store_executor.shutdown, True)

    # -- ownership -------------------------------------------------------------

    @staticmethod
    def _is_trial_holder(runtime: BrokerRuntime, holder: Holder, state: TrialState) -> bool:
        return any(op.binding.broker_id == runtime.broker_id
                   and (op.binding.client_id, op.binding.request_id) == (holder.client_id, holder.request_id)
                   for op in state.operations.values())

    def _exclusive(self, runtime: BrokerRuntime, state: TrialState) -> bool:
        """No holder other than the trial's own recorded acquisitions."""
        return all(self._is_trial_holder(runtime, holder, state) for holder in runtime.holders)

    @staticmethod
    def _recorded_requests(runtime: BrokerRuntime, state: TrialState) -> set[tuple[str, str]]:
        return {(op.binding.client_id, op.binding.request_id)
                for op in state.operations.values() if op.binding.broker_id == runtime.broker_id}

    def _classify(self, runtime: BrokerRuntime, state: TrialState, intent: str | None = None) -> str:
        """Who launched this runtime: 'ours', 'foreign' or 'unknown'.

        Never by time or fingerprint alone. The broker's immutable ``origin`` decides when it
        keeps one. Without it only a recorded holder can vouch, and any foreign holder makes it
        foreign. A runtime nobody can vouch for either way is 'unknown': it is never adopted or
        stopped, but it blocks cleanup proof, because it may be a launch that raced a cancel."""
        if runtime.root is None:
            return "foreign"
        if intent is not None:
            operations = [state.operations[intent]] if intent in state.operations else []
        else:
            operations = list(state.operations.values())
        candidates = [op for op in operations
                      if op.binding.broker_id == runtime.broker_id and runtime.root.created >= op.at]
        if runtime.launch_fingerprint != self.fingerprint or not candidates:
            return "foreign"
        wanted = {(op.binding.client_id, op.binding.request_id) for op in candidates}
        if runtime.origin is not None:
            return "ours" if (runtime.origin.client_id, runtime.origin.request_id) in wanted else "foreign"
        if any(not self._is_trial_holder(runtime, holder, state) for holder in runtime.holders):
            return "foreign"
        if any((holder.client_id, holder.request_id) in wanted for holder in runtime.holders):
            return "ours"
        return "unknown"

    def _owned(self, runtime: BrokerRuntime, manifest, state: TrialState) -> bool:
        """May the trial's stop be aimed at this runtime? A verified root (recorded, or provably
        launched by the trial) in the right broker incarnation, nobody else attached, and no
        provenance that points elsewhere."""
        if runtime.root is None or not self._exclusive(runtime, state):
            return False
        recorded = self._recorded_requests(runtime, state)
        if not recorded:
            return False  # not a broker incarnation the trial ever talked to
        if runtime.origin is not None and (runtime.origin.client_id, runtime.origin.request_id) not in recorded:
            return False
        return runtime.root in manifest or self._classify(runtime, state) == "ours"

    async def _tree(self, root: ProcessIdentity) -> set[ProcessIdentity]:
        return {root} | set(await self.process.descendants(root))

    async def _observe(self, state: TrialState, manifest) -> set[ProcessIdentity]:
        """Trees of every runtime that is recorded or provably the trial's (read-only)."""
        found: set[ProcessIdentity] = set()
        for runtime in await self.broker.runtimes(self.profile):
            if runtime.root is not None and (
                    runtime.root in manifest or self._classify(runtime, state) == "ours"):
                found |= await self._tree(runtime.root)
        return found

    async def _remember_tree(self, state: TrialState, manifest) -> bool:
        new = frozenset(manifest) - state.manifest
        if new:
            state.manifest = frozenset(state.manifest | new)
            await self._persist()
        return bool(new)

    async def _verify_ownership(self, manifest: frozenset[ProcessIdentity], state: TrialState) -> None:
        """Live identities must belong to a runtime the broker binds to the trial. Dead ones are
        harmless records (a dead identity can never match a live row)."""
        live = await self.process.live({p.pid for p in manifest})
        candidates = {p for p in manifest if live.get(p.pid) == p.created and p not in state.manifest}
        if not candidates:
            return
        verified: set[ProcessIdentity] = set()
        for runtime in await self.broker.runtimes(self.profile):
            if self._classify(runtime, state) == "ours":
                verified |= await self._tree(runtime.root)
        if not candidates <= verified:
            raise BackendError("manifest_not_trial_owned")

    async def discover_trial(self, intent: str) -> frozenset[ProcessIdentity]:
        state, found = await self._current(), set()
        if intent not in state.operations:
            raise BackendError("unknown_spawn_intent")
        for runtime in await self.broker.runtimes(self.profile):
            if self._classify(runtime, state, intent) == "ours":
                found |= await self._tree(runtime.root)
        return frozenset(found)

    async def _freeze_and_resolve(self, state: TrialState, manifest) -> set[ProcessIdentity]:
        """Observe, make it durable, freeze exactly the recorded producers, observe again, resolve.

        Observing *before* the cancel matters: cancelling can drop the very holder that
        identified a runtime. Observing *after* it catches a launch that raced the cancel
        (pending -> live). An unreported launch counts as resolved only when the broker says its
        acquisition is terminal; 'pending' or 'unknown' leaves it unresolved, and an unresolved
        launch blocks cleanup proof (it never degrades into an empty success)."""
        manifest = set(manifest)
        manifest |= await self._observe(state, manifest)
        await self._remember_tree(state, manifest)  # durable before anything is cancelled
        await self._cancel_recorded(state)
        outcomes = {}
        for intent, operation in list(state.operations.items()):
            if not operation.settled:
                outcomes[intent] = await self._attempt(
                    "acquisition_state", intent,
                    lambda binding=operation.binding: self.broker.acquisition_state(binding),
                    self.cancel_timeout)
        manifest |= await self._observe(state, manifest)  # after the terminal check: the final look
        resolved = [intent for intent, outcome in outcomes.items() if outcome == "terminal"]
        for intent in resolved:
            state.operations[intent] = replace(state.operations[intent], settled=True)
        # One save carries both the final tree and the resolutions.
        if not await self._remember_tree(state, manifest) and resolved:
            await self._persist()
        return manifest

    async def reconcile_startup(self) -> frozenset[ProcessIdentity]:
        state = await self._store_io(self.store.load)  # corrupt -> BackendError: stays closed, explicit
        self._state = state
        if state is None:
            return frozenset()
        if state.fingerprint:
            self.fingerprint = state.fingerprint  # the surviving trial's build, not this boot's guess
        self.embedding_tree = state.embedding
        manifest = set(state.manifest)
        if state.operations or manifest:
            manifest = await self._freeze_and_resolve(state, manifest)
        return frozenset(manifest)

    # -- release ---------------------------------------------------------------

    async def release_embedding(self) -> None:
        status = self.embedding.status()
        pid = status.get("pid") if status else None
        if pid:
            live = await self.process.live([pid])
            if pid in live:
                # Record the tree BEFORE unloading so absence can be proven (and survive a restart).
                self.embedding_tree |= await self._tree(ProcessIdentity(pid, live[pid], EMBEDDING_PROFILE))
                await self._current()
                await self._persist()
        result = await self.embedding.release()
        if result.get("reason") not in ("manual", "not_loaded"):
            raise BackendError(f"embedding_release_{result.get('reason')}")

    # -- cleanup ---------------------------------------------------------------

    async def _alive(self, identity: ProcessIdentity) -> bool:
        return (await self.process.live([identity.pid])).get(identity.pid) == identity.created

    async def _wait_gone(self, manifest, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            live = await self.process.live({p.pid for p in manifest})
            if all(live.get(p.pid) != p.created for p in manifest):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self.poll_interval)

    async def _attempt(self, name: str, key, call: Callable[[], Awaitable], timeout: float):
        """One port call with its own budget.

        A lost reply, an error or an overrun leaves the outcome uncertain: observation decides,
        not this. An overrunning call stays tracked (cancel is only requested) and is never
        repeated on top of itself by the next cleanup attempt."""
        slot = (name, key)
        prior = self._inflight.get(slot)
        if prior is not None and not prior.done():
            return "inflight"
        try:
            pending = call()
        except Exception:
            log.warning("GPU trial %s could not be issued", name, exc_info=True)
            return "failed"
        task = asyncio.ensure_future(pending)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._inflight[slot] = task
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            log.warning("GPU trial %s overran its %.0fs budget; outcome unknown", name, timeout)
            return "uncertain"
        self._inflight.pop(slot, None)
        if task.cancelled() or task.exception() is not None:
            log.warning("GPU trial %s was inconclusive", name)
            return "failed"
        return task.result()

    async def _cancel_recorded(self, state: TrialState) -> None:
        for intent, operation in list(state.operations.items()):
            await self._attempt("cancel_acquisition", intent,
                                lambda binding=operation.binding: self.broker.cancel_acquisition(binding),
                                self.cancel_timeout)

    async def cleanup_trial(self, manifest) -> None:
        state = await self._current()
        # Observe, freeze exactly the recorded producers, observe again: whatever is provably the
        # trial's - including a launch that raced the cancel - becomes a stop target too.
        manifest = frozenset(await self._freeze_and_resolve(state, set(manifest)))
        owned = []
        for runtime in await self.broker.runtimes(self.profile):
            if not self._owned(runtime, manifest, state):
                self.skipped.append(f"{runtime.profile}:not_trial_owned")
            elif await self._alive(runtime.root):  # a dead incarnation (or reused PID) is not asked to exit
                owned.append(runtime)
        for runtime in owned:
            await self._attempt("unload", runtime.root, lambda r=runtime: self.broker.request_unload(r),
                                self.unload_timeout)
        if owned:
            await self._wait_gone(manifest, self.unload_grace)
        # Formal stop only for what is *still* the trial's, re-verified at this moment.
        for runtime in await self.broker.runtimes(self.profile):
            if self._owned(runtime, manifest, state) and await self._alive(runtime.root):
                await self._attempt("formal_stop", runtime.root, lambda r=runtime: self.broker.formal_stop(r),
                                    self.stop_timeout)
        if owned:
            await self._wait_gone(manifest, self.stop_grace)

    # -- evidence --------------------------------------------------------------

    async def collect_evidence(self, nonce: str, manifest, *, clock: Callable[[], float],
                               purpose: str) -> CleanupEvidence:
        observed_at = clock()  # stamped before probing: a slow probe can only look staler, never fresher
        manifest = frozenset(manifest)
        state = await self._current()
        embedding_tree = self.embedding_tree
        runtimes = await self.broker.runtimes(self.profile)
        roots = {runtime.root.pid for runtime in runtimes if runtime.root is not None}
        live = await self.process.live({p.pid for p in manifest | embedding_tree} | roots)
        absent = frozenset(p for p in manifest if live.get(p.pid) != p.created)
        embedding_absent = all(live.get(p.pid) != p.created for p in embedding_tree)
        status = self.embedding.status()
        if status and status.get("process_running"):
            embedding_absent = False
        unrecorded = False
        for process in manifest - absent:
            if await self.process.descendants(process) - manifest:
                unrecorded = True  # a late child nobody recorded: the loader is not settled
        # Only the trial's own acquisitions can keep its producer alive; a foreign pending
        # acquisition of the same profile is not ours to wait for or to cancel.
        pending = any(holder.pending and self._is_trial_holder(runtime, holder, state)
                      for runtime in runtimes for holder in runtime.holders)
        # A launch whose outcome was never reconciled, a live runtime that is the trial's but not
        # in the manifest, and a live runtime nobody can attribute (it may be a launch that raced a
        # cancel): none of these may become "nothing there".
        unresolved = any(not operation.settled for operation in state.operations.values())
        unaccounted = any(
            runtime.root is not None and live.get(runtime.root.pid) == runtime.root.created
            and runtime.root not in manifest and self._classify(runtime, state) in ("ours", "unknown")
            for runtime in runtimes)
        loader_settled = not (unrecorded or pending or unresolved or unaccounted)
        capacity_available = await self.capacity.free_bytes() >= self.required_free_bytes[purpose]
        return CleanupEvidence(expected=manifest, absent=absent, loader_settled=loader_settled,
                               embedding_tree_absent=embedding_absent,
                               capacity_available=capacity_available, nonce=nonce, observed_at=observed_at)
