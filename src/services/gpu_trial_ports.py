"""Concrete OS, GPU and broker ports for the cooperative GPU trial backend.

``gpu_trial_adapter`` decides what may be touched and what counts as proof; this module only
*observes and asks*, through the existing machinery, and never invents evidence:

* ``PsutilProcessPort`` - process incarnations are (pid, create_time). A process that cannot be
  read is an error, never "absent"; nothing here can terminate a process.
* ``NvidiaSmiCapacityPort`` - a fresh ``nvidia-smi`` reading every call. A missing binary, a
  non-zero exit, a timeout or unparseable output raises; it is never read as free memory.
* ``ManagedBrokerPort`` - the shared local-runtime broker is the single owner of the trial's
  runtime. Ownership is the broker's own recorded launch provenance (opaque acquisition digests
  that only this process's recorded bindings can be mapped back to), cancellation is by the
  exact (client, request) acquisition, and a stop is fenced to one runtime incarnation by the
  broker itself. A broker that does not advertise that provenance is refused outright: without
  it nothing can be attributed, and guessing is how foreign runtimes get stopped.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Iterable, Sequence
from urllib.parse import urlsplit

import psutil

from ..shared_local_runtime import acquisition_digest
from ..shared_local_runtime.client import Client, SharedRuntimeError
from .gpu_admission import ProcessIdentity
from .gpu_admission_driver import BackendError, LaunchBinding
from .gpu_trial_adapter import BrokerRuntime, Holder, Origin

log = logging.getLogger(__name__)

REQUIRED_BROKER_CAPABILITIES = frozenset({"acquisition-provenance", "incarnation-fenced-stop"})
# Side-effecting broker calls run here, not in the loop's default executor, so they can neither be
# starved by unrelated work nor outlive the process silently (every call has an HTTP timeout).
_ACTIONS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gpu-trial-port")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Not a UUID, so it can never equal a recorded launch identity: a provenance this process cannot
# map back is somebody else's.
FOREIGN = "<foreign>"


class PsutilProcessPort:
    """Read-only view of the OS process table."""

    async def live(self, pids: Iterable[int]) -> dict[int, float]:
        return await asyncio.to_thread(self._live, frozenset(pids))

    async def descendants(self, root: ProcessIdentity) -> frozenset[ProcessIdentity]:
        return await asyncio.to_thread(self._descendants, root)

    @staticmethod
    def _live(pids: frozenset[int]) -> dict[int, float]:
        found: dict[int, float] = {}
        for pid in pids:
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                continue
            try:
                process = psutil.Process(pid)
                created = process.create_time()
            except psutil.NoSuchProcess:  # includes a zombie: it can no longer hold the GPU
                continue
            except psutil.Error as exc:
                raise BackendError("process_probe_failed") from exc  # unreadable is not absent
            found[pid] = created
        return found

    @staticmethod
    def _descendants(root: ProcessIdentity) -> frozenset[ProcessIdentity]:
        try:
            process = psutil.Process(root.pid)
            if process.create_time() != root.created:
                return frozenset()  # that incarnation is gone; a reused pid's children are not its own
            children = process.children(recursive=True)
        except psutil.NoSuchProcess:
            return frozenset()
        except psutil.Error as exc:
            raise BackendError("process_probe_failed") from exc
        found = set()
        for child in children:
            try:
                found.add(ProcessIdentity(child.pid, child.create_time(), root.profile))
            except psutil.NoSuchProcess:
                continue  # exited while being listed
            except psutil.Error as exc:
                raise BackendError("process_probe_failed") from exc
        return frozenset(found)


class NvidiaSmiCapacityPort:
    """Free GPU memory straight from ``nvidia-smi``; the minimum over the watched devices.

    With no ``indices`` every device is watched and the smallest free amount wins, so an
    unexpectedly busy device makes proof fail rather than pass."""

    QUERY = ("--query-gpu=index,memory.free", "--format=csv,noheader,nounits")

    def __init__(self, *, command: Sequence[str] = ("nvidia-smi",), indices: Iterable[int] | None = None,
                 timeout: float = 10.0):
        self.command = tuple(command)
        self.indices = None if indices is None else frozenset(int(index) for index in indices)
        self.timeout = float(timeout)

    async def free_bytes(self) -> int:
        return await asyncio.to_thread(self._free_bytes)

    def _free_bytes(self) -> int:
        try:
            completed = subprocess.run(
                [*self.command, *self.QUERY], capture_output=True, text=True, timeout=self.timeout,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as exc:
            raise BackendError("capacity_probe_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("capacity_probe_timeout") from exc
        except OSError as exc:
            raise BackendError("capacity_probe_unavailable") from exc
        if completed.returncode != 0:
            raise BackendError("capacity_probe_failed")
        return self._parse(completed.stdout)

    def _parse(self, output: str) -> int:
        free: dict[int, int] = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise BackendError("capacity_probe_invalid")
            index, mebibytes = int(parts[0]), int(parts[1])
            if index in free:
                raise BackendError("capacity_probe_invalid")
            free[index] = mebibytes
        watched = free if self.indices is None else {index: free[index] for index in self.indices if index in free}
        if not free or not watched or (self.indices is not None and set(watched) != set(self.indices)):
            raise BackendError("capacity_probe_invalid")
        return min(watched.values()) * 1024 * 1024


async def _action(function, *args):
    """Run one blocking, side-effecting call to its real end.

    Cancelling the awaiting task is a *request*: the call may already be inside the broker or the
    runtime, so the task stays in flight until the thread really returns (then the cancel is
    honoured). The adapter tracks the task as the operation, so an overrunning call is never
    repeated on top of itself and never mistaken for completion."""
    future = asyncio.wrap_future(_ACTIONS.submit(function, *args))
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True  # a request only: the real call is still running
        except BaseException:
            break  # the call itself failed; reported below once it is over
    if cancelled:
        if not future.cancelled():
            future.exception()  # consumed: nobody is left to observe it
        raise asyncio.CancelledError()
    return future.result()


def _loopback_base(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS or parsed.username or parsed.password:
        raise BackendError("unload_endpoint_not_loopback")  # a bearer key is never sent anywhere else
    base = endpoint.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _tcp_endpoint(address) -> tuple[str, int] | None:
    """One TCP endpoint as ``(host, port)``, from a socket tuple or a ``psutil`` address.

    ``None`` means "cannot be compared with anything": a missing or malformed endpoint is never
    evidence of identity, so a caller that needs a match must fail closed on it.
    """
    if address is None:
        return None
    if isinstance(address, (tuple, list)):
        if len(address) < 2:
            return None
        host, port = address[0], address[1]
    else:
        host, port = getattr(address, "ip", None), getattr(address, "port", None)
    if not isinstance(host, str) or not host or not isinstance(port, int) or isinstance(port, bool):
        return None
    if not 0 < port < 65536:
        return None
    return host.split("%", 1)[0].lower(), port  # drop an IPv6 scope suffix: the address is the identity


class ManagedBrokerPort:
    """The shared local-runtime broker as the trial's runtime owner.

    ``client_factory`` must return the *same* client the trial launch acquires with: the broker
    only lets a client cancel its own acquisitions. ``known_bindings`` yields the launch
    bindings this process recorded; the broker publishes only opaque digests of (broker, client,
    request), so exactly those bindings, and nothing else, can be recognised."""

    def __init__(self, *, client_factory: Callable[[], Client],
                 known_bindings: Callable[[], Iterable[LaunchBinding]] = lambda: (),
                 api_key: Callable[[], str] = lambda: "", unload_http_timeout: float = 80.0):
        self.client_factory = client_factory
        self.known_bindings = known_bindings
        self.api_key = api_key
        self.unload_http_timeout = float(unload_http_timeout)

    # -- reading ----------------------------------------------------------------

    def _read_status(self) -> dict:
        client = self.client_factory()
        try:
            capabilities = client.connect(start_if_missing=False)  # a read never bootstraps a broker
            status = client.status()
        except SharedRuntimeError as exc:
            raise BackendError("broker_unavailable") from exc
        if not REQUIRED_BROKER_CAPABILITIES <= set(capabilities.get("capabilities", ())):
            raise BackendError("broker_provenance_unsupported")
        if not isinstance(status.get("broker_id"), str) or not status["broker_id"]:
            raise BackendError("broker_status_invalid")
        return status

    async def _status(self) -> dict:
        return await asyncio.to_thread(self._read_status)

    def _rows(self, profile: str, status: dict) -> list[BrokerRuntime]:
        broker_id = status["broker_id"]
        known = {acquisition_digest(binding.broker_id, binding.client_id, binding.request_id): binding
                 for binding in self.known_bindings() if binding.broker_id == broker_id}

        def holder(acq, *, pending: bool) -> Holder:
            binding = known.get(acq) if isinstance(acq, str) else None
            if binding is None:
                return Holder(FOREIGN, f"digest:{acq}" if acq else "unattributed", pending)
            return Holder(binding.client_id, binding.request_id, pending)

        rows: list[BrokerRuntime] = []
        for item in status.get("runtimes", ()):
            if item.get("profile_id") != profile:
                continue
            root = None
            if item.get("running") and item.get("pid"):
                if not isinstance(item.get("create_time"), (int, float)):
                    raise BackendError("runtime_identity_unreadable")  # alive but unreadable is not "gone"
                root = ProcessIdentity(int(item["pid"]), float(item["create_time"]), profile)
            origin = item.get("origin")
            if origin is None:
                provenance = None
            else:
                binding = known.get(origin)
                provenance = (Origin(binding.client_id, binding.request_id) if binding is not None
                              else Origin(FOREIGN, f"digest:{origin}"))
            rows.append(BrokerRuntime(
                profile=profile, root=root, launch_fingerprint=str(item.get("launch_fingerprint") or ""),
                broker_id=broker_id,
                holders=frozenset(holder(entry.get("acq"), pending=False) for entry in item.get("holders", ())),
                origin=provenance, endpoint=item.get("base_url"), runtime_kind=str(item.get("runtime") or "")))
        for item in status.get("pending_acquisitions", ()):
            if item.get("profile_id") == profile:
                rows.append(BrokerRuntime(profile=profile, root=None, launch_fingerprint="", broker_id=broker_id,
                                          holders=frozenset({holder(item.get("acq"), pending=True)})))
        return rows

    async def runtimes(self, profile: str) -> list[BrokerRuntime]:
        return self._rows(profile, await self._status())

    async def acquisition_state(self, binding: LaunchBinding) -> str:
        status = await self._status()
        if binding.broker_id != status["broker_id"]:
            return "unknown"  # another broker incarnation never reports on this one's acquisitions
        digest = acquisition_digest(binding.broker_id, binding.client_id, binding.request_id)
        if any(item.get("acq") == digest for item in status.get("pending_acquisitions", ())):
            return "pending"
        if any(item.get("acq") == digest for item in status.get("finished_acquisitions", ())):
            return "terminal"
        return "unknown"  # not seen yet (still queued for the lifecycle lock), forgotten, or never sent

    # -- acting -----------------------------------------------------------------

    async def cancel_acquisition(self, binding: LaunchBinding) -> None:
        client = self.client_factory()
        if binding.client_id != client.client_id:
            # The broker lets a client end only its own acquisitions. A previous process's launch
            # ends when the broker reaps that dead client; ``acquisition_state`` reports it then.
            raise BackendError("acquisition_owned_by_another_client")
        if not await _action(partial(client.cancel_request, binding.request_id, broker_id=binding.broker_id)):
            raise BackendError("cancel_unconfirmed")

    async def request_unload(self, runtime: BrokerRuntime) -> str:
        if runtime.runtime_kind != "strata" or not runtime.endpoint:
            return "lost"  # no normal-exit request exists for this runtime: the formal stop decides
        base = _loopback_base(runtime.endpoint)
        if runtime.root is None:
            raise BackendError("unload_needs_verified_root")
        return await _action(self._post_unload, base, runtime.root)

    @staticmethod
    def _verify_listener(connection: socket.socket, root: ProcessIdentity) -> None:
        """The process that *accepted* ``connection`` must be the verified runtime's own tree.

        The endpoint is only an address: another incarnation (or a foreign program) may have taken
        it since the broker's snapshot, and an unload sent there cannot be taken back. A LISTEN
        socket on that port proves nothing about *this* connection - it can belong to a process
        that grabbed the port after the real listener closed it, while the peer that already
        accepted this connection still holds it and would still answer our POST. So the evidence is
        the accepted half of this exact TCP 4-tuple: the table entry whose local address is this
        connection's peer and whose remote address is this connection's own local address. That
        entry names the process holding the accepted socket, and only its own (pid, create_time)
        may authorise a send. Unknown, missing, ambiguous or foreign owners fail closed."""
        try:
            peer = _tcp_endpoint(connection.getpeername())
            local = _tcp_endpoint(connection.getsockname())
        except OSError as exc:
            raise BackendError("unload_target_unverified") from exc
        if peer is None or local is None:
            raise BackendError("unload_target_unverified")  # an unnamed end of a socket proves nothing
        try:
            accepted = [entry for entry in psutil.net_connections(kind="tcp")
                        if entry.status == psutil.CONN_ESTABLISHED
                        and _tcp_endpoint(entry.laddr) == peer and _tcp_endpoint(entry.raddr) == local]
            owners = {entry.pid for entry in accepted}
            runtime = psutil.Process(root.pid)
            if runtime.create_time() != root.created:
                raise BackendError("unload_target_changed")  # that pid is another incarnation now
            allowed = {(root.pid, root.created)}
            allowed |= {(child.pid, child.create_time()) for child in runtime.children(recursive=True)}
        except BackendError:
            raise
        except psutil.NoSuchProcess as exc:
            raise BackendError("unload_target_changed") from exc
        except psutil.Error as exc:
            raise BackendError("unload_target_unverified") from exc
        if len(owners) != 1 or any(not isinstance(pid, int) or pid <= 0 for pid in owners):
            raise BackendError("unload_target_unverified")  # nobody, several, or an owner nobody names
        (owner,) = owners
        try:
            created = psutil.Process(owner).create_time()
        except psutil.NoSuchProcess as exc:
            raise BackendError("unload_target_changed") from exc
        except psutil.Error as exc:
            raise BackendError("unload_target_unverified") from exc
        if (owner, created) not in allowed:
            raise BackendError("unload_target_changed")  # another process accepted our connection

    def _post_unload(self, base: str, root: ProcessIdentity) -> str:
        parsed = urlsplit(base)
        key = self.api_key()
        authorization = f"Authorization: Bearer {key}\r\n" if key else ""
        request = (f"POST /v1/unload HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n"
                   f"{authorization}\r\n{{}}").encode("ascii")
        deadline = time.monotonic() + self.unload_http_timeout
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as connection:
                self._verify_listener(connection, root)  # accepted-socket owner, before any byte
                connection.settimeout(1.0)
                connection.sendall(request)
                reply = b""
                while b"\r\n" not in reply:
                    if time.monotonic() >= deadline:
                        return "lost"
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        return "lost"
                    reply += chunk
        except OSError:
            return "lost"  # a lost reply or a timeout proves nothing: observation decides
        parts = reply.split(b"\r\n", 1)[0].split(b" ")
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if 200 <= status < 300:
            return "ok"
        return "busy" if status == 409 else "lost"

    async def formal_stop(self, runtime: BrokerRuntime) -> int:
        if runtime.root is None:
            raise BackendError("stop_needs_verified_root")
        return await _action(self._stop, runtime)

    def _stop(self, runtime: BrokerRuntime) -> int:
        client = self.client_factory()
        try:
            client.connect(start_if_missing=False)
            if client.broker_id != runtime.broker_id:
                raise BackendError("stale_runtime_view")
            reply = client.stop_runtime(
                runtime.profile, expect_pid=runtime.root.pid, expect_created=runtime.root.created,
                expect_launch_fingerprint=runtime.launch_fingerprint)
        except SharedRuntimeError as exc:
            log.warning("GPU trial formal stop was refused: %s", exc)
            raise BackendError("formal_stop_refused") from exc
        return int(reply.get("stopped", 0))
