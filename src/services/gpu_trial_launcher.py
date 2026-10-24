"""Trial launch under a HELD GPU lease: the real loader producer and its lifecycle.

The lease only decides *when* the GPU is free. This launcher is the one place that starts the
trial runtime while it is held, and it makes every step the driver and adapter rely on true:

* The spawn intent (with its broker binding) is durable *before* the acquisition is sent, through
  the caller-held ``SpawnScope``; the acquisition uses exactly that binding's request id.
* The producer is a real thread: it acquires from the shared broker, then asks the runtime to load.
  ``producer_settled`` is that thread's real completion - cancelling the coroutine that awaits it
  settles nothing. A failed or cancelled producer additionally needs the broker to report its
  acquisition terminal before it counts as settled: an HTTP error does not prove the broker
  stopped working on the request.
* A lease that ends (cancel, expiry) cancels exactly this producer: its pending acquisition at
  the broker and, if the load request is in flight, that wait (the thread notices within one
  poll). It never touches another caller's acquisition or any process; stopping the runtime is
  the adapter's cleanup.
* The trial's own consumer lease is released only after the producer is done; the broker then
  stops a runtime nobody holds, and the adapter independently verifies it is gone.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from ..shared_local_runtime.client import Client
from .gpu_admission import Conflict
from .gpu_admission_driver import BackendError, GpuAdmissionDriver, LaunchBinding, SpawnScope
from .gpu_trial_ports import LOOPBACK_HOSTS, REQUIRED_BROKER_CAPABILITIES

log = logging.getLogger(__name__)

MAX_LAUNCH_RECORDS = 32
LOAD_POLL_SECONDS = 0.2  # how often the load wait looks at the cancel flag


class _Aborted(Exception):
    """The lease ended while this producer was running."""


class _Producer:
    """The real thread that can still create runtime descendants for one launch.

    It also is the ``cancel_handle`` the shared client understands (``cancel_requested`` plus
    ``add_cancel_callback``), so a pending acquisition is cancelled at the broker by request id."""

    def __init__(self):
        self.started = False     # the thread exists; before that there is nothing to wait for
        self.done = threading.Event()
        self.cancel_requested = threading.Event()
        self.confirmed = False   # the broker reported the acquisition terminal (or answered it)
        # The load request is a *remote* producer: once it has been sent, ending this thread (a closed
        # connection, an abort) says nothing about the runtime still loading. It is unknown until the
        # runtime answered it, and is settled only by that runtime being gone.
        self.remote_load_unknown = False
        self.remote_settled = False
        self.ticket = None
        self.reply = None
        self.error: str | None = None
        self.phase = "queued"
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def add_cancel_callback(self, callback):
        with self._lock:
            invoke = self.cancel_requested.is_set()
            if not invoke:
                self._callbacks.append(callback)
        if invoke:
            callback()

        def remove():
            with self._lock:
                self._callbacks = [item for item in self._callbacks if item is not callback]

        return remove

    def request_cancel(self) -> None:
        """Ask this producer to stop: a pending acquisition is cancelled at the broker by request id
        (the client's callback), and ``post_load`` notices the flag within one poll."""
        with self._lock:
            self.cancel_requested.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.debug("GPU trial cancel callback failed", exc_info=True)

    def settled(self) -> bool:
        """True only once nothing of this launch can still create a process: the thread is gone, the
        broker can no longer start anything for its acquisition, and a load request that was sent
        and never answered has been ended with the runtime that ran it."""
        return (self.done.is_set() and self.confirmed
                and (not self.remote_load_unknown or self.remote_settled))


@dataclass
class LaunchRecord:
    owner: str
    operation_id: str
    fence: tuple[str, str, str]
    state: str = "starting"          # starting | acquiring | loading | held | failed | aborted | closed
    error: str | None = None
    producer: _Producer = field(default_factory=_Producer)
    task: asyncio.Task | None = None
    binding: LaunchBinding | None = None
    intent: str | None = None
    confirm_tried: bool = False
    remote_tried: bool = False
    closing: bool = False  # the server is shutting down: restart recovery, not this task, finishes the job

    @property
    def finished(self) -> bool:
        return self.state in ("failed", "aborted", "closed")


def post_load(base_url: str, api_key: str, producer: _Producer, timeout: float) -> None:
    """Ask the runtime to load its model, abortable at any moment through ``producer``.

    The wait is a short-timeout read loop that watches the cancel flag: on Windows closing or
    shutting down a socket another thread is blocked on does not wake it, so the thread itself
    must notice. The answer's status line is all that is needed (the reply comes after the load)."""
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS or parsed.username or parsed.password:
        raise BackendError("load_endpoint_not_loopback")  # the bearer key is never sent elsewhere
    authorization = f"Authorization: Bearer {api_key}\r\n" if api_key else ""
    request = (f"POST /load HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n"
               f"{authorization}\r\n{{}}").encode("ascii")
    deadline = time.monotonic() + timeout
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as connection:
        connection.settimeout(LOAD_POLL_SECONDS)
        connection.sendall(request)
        # From here the runtime may be loading on our behalf. Dropping the connection (abort, timeout,
        # reset) does not stop it, so the flag stays set until the runtime has actually answered.
        producer.remote_load_unknown = True
        reply = b""
        while b"\r\n" not in reply:
            if producer.cancel_requested.is_set():
                raise _Aborted()
            if time.monotonic() >= deadline:
                raise BackendError("load_timeout")
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise BackendError("load_connection_closed")
            reply += chunk
    producer.remote_load_unknown = False  # answered (success or refusal): the load request is over
    parts = reply.split(b"\r\n", 1)[0].split(b" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if not 200 <= status < 300:
        raise BackendError(f"load_http_{status}")


class GpuTrialLauncher:
    """Starts and supervises one trial runtime per held lease."""

    def __init__(self, driver: GpuAdmissionDriver, broker, *, profile: str,
                 client_factory: Callable[[], Client], snapshot_factory: Callable[[], str | None],
                 api_key: Callable[[], str] = lambda: "", load_timeout: float = 600.0,
                 poll_interval: float = 0.1, confirm_timeout: float = 60.0, end_timeout: float = 90.0,
                 remote_timeout: float = 240.0,
                 load: Callable[[str, str, _Producer, float], None] = post_load):
        self.driver = driver
        self.broker = broker
        self.profile = profile
        self.client_factory = client_factory
        self.snapshot_factory = snapshot_factory
        self.api_key = api_key
        self.load_timeout = load_timeout
        self.poll_interval = poll_interval
        self.confirm_timeout = confirm_timeout
        self.end_timeout = end_timeout
        self.remote_timeout = remote_timeout
        self.load = load
        self._launches: OrderedDict[tuple[str, str], LaunchRecord] = OrderedDict()

    # -- observation -----------------------------------------------------------

    @staticmethod
    def _view(launch: LaunchRecord) -> dict:
        """Observational only: never a token, client id or process."""
        producer = launch.producer
        return {"launch_id": launch.operation_id, "state": launch.state, "error": launch.error,
                "phase": producer.phase, "producer_done": producer.done.is_set(),
                "producer_settled": producer.settled()}

    def status(self) -> dict | None:
        for launch in reversed(self._launches.values()):
            return self._view(launch)
        return None

    # -- launch -----------------------------------------------------------------

    def _prepare(self):
        """Everything that may fail before any launch exists (nothing external is started)."""
        client = self.client_factory()
        capabilities = client.connect()  # may start the broker, exactly like any managed launch
        if not REQUIRED_BROKER_CAPABILITIES <= set(capabilities.get("capabilities", ())):
            raise Conflict("broker_provenance_unsupported")
        snapshot_id = self.snapshot_factory()
        verdict = client.validate(self.profile, snapshot_id)
        fingerprint = verdict.get("launch_fingerprint") if isinstance(verdict, dict) else None
        if not isinstance(fingerprint, str) or not fingerprint:
            raise BackendError("launch_fingerprint_unavailable")
        return client, capabilities["broker_id"], snapshot_id, fingerprint

    async def launch(self, owner: str, token: str, boot: str, operation_id: str) -> dict:
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            raise ValueError("invalid launch")
        coordinator = self.driver.coordinator
        fence = (owner, token, boot)
        key = (owner, operation_id)
        # Everything up to the reservation is synchronous: no other call can interleave between the
        # checks and the record, so concurrent calls (same or different operation id) cannot both pass.
        existing = self._launches.get(key)
        if existing is not None:
            if existing.fence != fence:
                raise Conflict("operation payload changed")
            return self._view(existing)  # same operation id: the same launch, never a second one
        if coordinator.driver_fence() != fence:
            raise Conflict("stale ownership")
        if any(not launch.finished for launch in self._launches.values()):
            raise Conflict("launch_in_progress")
        if any(launch.fence == fence and launch.binding is not None for launch in self._launches.values()):
            raise Conflict("lease_already_launched")  # one launch per lease; cancel and lease again to retry
        backend = self.driver.backend
        if backend is None or not hasattr(backend, "bind_fingerprint"):
            raise Conflict("gpu_lease_backend_unavailable")
        launch = LaunchRecord(owner, operation_id, fence)
        self._launches[key] = launch
        while len(self._launches) > MAX_LAUNCH_RECORDS:
            for old_key, old in self._launches.items():
                if old.finished:
                    del self._launches[old_key]
                    break
            else:
                break
        try:
            client, broker_id, snapshot_id, fingerprint = await asyncio.to_thread(self._prepare)
            if coordinator.driver_fence() != fence:
                raise Conflict("stale ownership")  # the lease ended while the launch was prepared
            backend.bind_fingerprint(fingerprint)
        except BaseException as exc:
            # Nothing external was started, so nothing is open; a replay of this operation id sees why.
            launch.state = "failed"
            launch.error = str(exc) if isinstance(exc, (BackendError, Conflict)) else type(exc).__name__
            raise
        launch.binding = LaunchBinding(broker_id, client.client_id, str(uuid.uuid4()))
        launch.task = asyncio.create_task(self._supervise(launch, client, snapshot_id),
                                          name="gpu-trial-launch")
        launch.task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return self._view(launch)

    # -- the producer thread ----------------------------------------------------

    def _produce(self, launch: LaunchRecord, client: Client, snapshot_id: str | None) -> None:
        producer = launch.producer
        try:
            producer.phase = "acquiring"
            ticket, reply = client.acquire(
                self.profile, kind="consumer", snapshot_id=snapshot_id,
                request_id=launch.binding.request_id, cancel_handle=producer)
            producer.ticket, producer.reply = ticket, reply
            if producer.cancel_requested.is_set():
                raise _Aborted()
            producer.phase = "loading"
            self.load(reply["base_url"], self.api_key(), producer, self.load_timeout)
            if producer.cancel_requested.is_set():
                raise _Aborted()
            producer.phase = "loaded"
            producer.confirmed = True  # the broker answered the acquisition: it is over
        except BaseException as exc:
            producer.phase = "failed"
            producer.error = "aborted" if isinstance(exc, _Aborted) or producer.cancel_requested.is_set() else (
                str(exc) if isinstance(exc, BackendError) else type(exc).__name__)
        finally:
            if producer.cancel_requested.is_set():
                self._release_ticket(producer)  # a cancelled producer cleans up its own lease first
            producer.done.set()  # last: "done" must mean nothing of this thread can still happen

    @staticmethod
    def _release_ticket(producer: _Producer) -> None:
        ticket, producer.ticket = producer.ticket, None
        if ticket is None:
            return
        try:
            ticket.release()  # the broker stops a runtime nobody holds; the adapter verifies it
        except Exception:
            log.warning("GPU trial lease could not be released; the broker retries via the client", exc_info=True)

    # -- supervision ------------------------------------------------------------

    def _lease_is_ours(self, launch: LaunchRecord) -> bool:
        coordinator = self.driver.coordinator
        return coordinator.driver_fence() == launch.fence and coordinator.status()["phase"] == "HELD"

    async def _wait_for_producer(self, launch: LaunchRecord) -> None:
        producer = launch.producer
        while not producer.done.is_set():
            if not producer.cancel_requested.is_set() and not self._lease_is_ours(launch):
                producer.request_cancel()  # the lease ended: stop exactly this producer
            await asyncio.sleep(self.poll_interval)

    async def _confirm_terminal(self, launch: LaunchRecord) -> None:
        """A failed producer is settled only once the broker says its acquisition is terminal."""
        producer, loop = launch.producer, asyncio.get_running_loop()
        launch.confirm_tried = True
        deadline = loop.time() + self.confirm_timeout
        while not producer.confirmed:
            state = await self.broker.acquisition_state(launch.binding)
            if state == "terminal":
                producer.confirmed = True
                return
            if state == "pending":
                try:
                    await self.broker.cancel_acquisition(launch.binding)
                except BackendError:
                    log.warning("GPU trial acquisition cancel was not confirmed")
            if loop.time() >= deadline:
                return  # stays unsettled: explicit ERROR, never an empty success
            await asyncio.sleep(self.poll_interval)

    async def _settle_remote(self, launch: LaunchRecord) -> None:
        """A load request that was sent and never answered may still be running inside the runtime.

        Closing our end of the connection does not stop it, and the runtime can still start its
        engine later. Only that runtime's end settles it, so it is ended here through the same
        ownership-verified cleanup the driver uses (the trial's own runtime only: unload, then the
        fenced formal stop), and the launch settles once no runtime of this launch is alive. If it
        cannot be ended in time the producer stays unsettled: an explicit ERROR, never a guess."""
        producer, backend = launch.producer, self.driver.backend
        if producer.remote_settled or launch.intent is None:
            return
        launch.remote_tried = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.remote_timeout
        while not producer.remote_settled:
            try:
                await backend.cleanup_trial(frozenset())
                if not await backend.discover_trial(launch.intent):
                    producer.remote_settled = True
                    return
            except BackendError as exc:
                log.warning("GPU trial runtime could not be ended yet: %s", exc)
            if loop.time() >= deadline:
                return  # still unsettled
            await asyncio.sleep(self.poll_interval)

    async def _settle(self, scope: SpawnScope, launch: LaunchRecord) -> None:
        manifest = await self.driver.backend.discover_trial(scope.intent)
        await scope.settle(manifest)

    async def _supervise(self, launch: LaunchRecord, client: Client, snapshot_id: str | None) -> None:
        producer = launch.producer
        try:
            scope = self.driver.spawn_scope(*launch.fence, binding=launch.binding, producer_settled=producer.settled)
            async with scope:  # the intent is durable before the acquisition is sent
                launch.intent = scope.intent
                producer.started = True
                threading.Thread(target=self._produce, args=(launch, client, snapshot_id), daemon=True,
                                 name="gpu-trial-producer").start()
                launch.state = "loading"
                await self._wait_for_producer(launch)
                if producer.error is not None:
                    launch.error = producer.error
                    await self._confirm_terminal(launch)
                    if producer.remote_load_unknown:
                        await self._settle_remote(launch)
                if producer.settled():
                    try:
                        await self._settle(scope, launch)
                    except Conflict:
                        pass  # the lease is gone: the driver's recovery settles this intent
            if producer.error is not None:
                launch.state = "aborted" if producer.error == "aborted" else "failed"
                return
            launch.state = "held"
            while self._lease_is_ours(launch):
                await asyncio.sleep(self.poll_interval)
            launch.state = "closed"
        except asyncio.CancelledError:
            launch.state = "aborted"
            raise
        except Exception as exc:
            launch.state = "failed"
            launch.error = launch.error or (str(exc) if isinstance(exc, (BackendError, Conflict)) else type(exc).__name__)
            log.warning("GPU trial launch ended with %s", launch.error)
        finally:
            await self._end(launch)

    async def _end(self, launch: LaunchRecord) -> None:
        """Whatever happened, no producer outlives its launch and no lease of ours is left held."""
        producer = launch.producer
        if not producer.started:
            producer.done.set()  # the intent never opened: no thread exists to wait for
            return
        if not producer.done.is_set():
            producer.request_cancel()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.end_timeout
            while not producer.done.is_set() and loop.time() < deadline:
                await asyncio.sleep(self.poll_interval)
            if not producer.done.is_set():
                log.warning("GPU trial producer did not finish within %.0fs of the cancel", self.end_timeout)
                return  # the producer thread releases its own lease when it finally ends
        if not producer.confirmed and not launch.confirm_tried and not launch.closing:
            # This task ended before the broker's answer was awaited (an unexpected error, or a
            # cancel): the driver's recovery of the open intent needs the producer settled, and
            # only this confirmation can make it so.
            try:
                await self._confirm_terminal(launch)
            except Exception:
                log.warning("GPU trial acquisition could not be confirmed terminal", exc_info=True)
        if producer.ticket is not None:
            await asyncio.to_thread(self._release_ticket, producer)
        if producer.remote_load_unknown and not producer.remote_settled and not launch.remote_tried \
                and not launch.closing:
            try:
                await self._settle_remote(launch)
            except Exception:
                log.warning("GPU trial runtime could not be ended after its launch task", exc_info=True)

    async def aclose(self) -> None:
        """Server shutdown: stop every producer; their intents stay owned by the driver.

        Nothing is awaited from the broker here: the durable state file and the next startup's
        reconciliation own whatever is still unresolved."""
        tasks = [launch.task for launch in self._launches.values() if launch.task and not launch.task.done()]
        for launch in self._launches.values():
            launch.closing = True
            if not launch.producer.done.is_set():
                launch.producer.request_cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
