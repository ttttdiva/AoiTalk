"""Native client with opaque leases and no direct-spawn fallback."""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from . import DEFAULT_URL, PROTOCOL_VERSION

log = logging.getLogger(__name__)


class SharedRuntimeError(RuntimeError):
    pass


class SharedRuntimeAcquisitionSuperseded(SharedRuntimeError):
    """A route warm-up was cancelled because a newer selection replaced it."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RemoteTicket:
    def __init__(self, client, reply, kind, holder=None, *, snapshot_id=None):
        self.client = client
        self.endpoint = reply["base_url"]
        self.alias = reply["model"]
        self.ticket_id = reply["ticket"]
        self.broker_id = reply["broker_id"]
        self.profile_id = reply["profile_id"]
        # Keep the frozen launch identity beside the opaque lease.  The shared
        # bridge may be asked to ensure a long-lived holder repeatedly; it must
        # never reuse a ticket created from a different snapshot.
        self.snapshot_id = snapshot_id
        self.launch_fingerprint = reply.get("launch_fingerprint")
        self.request_id = None  # the acquisition that produced this lease (set by Client.acquire)
        self.kind = kind
        self.consumer = "shared-client"
        self._released = False
        self._holder = holder
        self._holder_attribute = "_llama_cpp_generation_lease_tickets" if kind == "generation" else "_llama_cpp_runtime_consumer_lease_tickets"
        if holder is not None:
            tickets = getattr(holder, self._holder_attribute, None)
            if not isinstance(tickets, set):
                tickets = set()
                setattr(holder, self._holder_attribute, tickets)
            tickets.add(self)

    def release(self):
        self.client.release(self)


class Client:
    def __init__(self, *, url=DEFAULT_URL, autostart=True):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Shared runtime control URL must be IPv4 loopback HTTP")
        self.url = url.rstrip("/")
        self.autostart = autostart
        self.client_id = str(uuid.uuid4())
        self.broker_id = None
        self.pid = os.getpid()
        self.tickets = {}
        self.pending = {}
        self._pending_acquires = {}
        self.cancel_pending = set()
        self.release_pending = set()
        self._cancel_worker = None
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.closed = threading.Event()
        self._heartbeat_thread = None
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        atexit.register(self.close)

    def _request(self, path, data=None, *, timeout=180):
        raw = None if data is None else json.dumps(data, allow_nan=False).encode()
        request = Request(self.url + path, data=raw, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise SharedRuntimeError("Coordinator response exceeded its limit")
                return json.loads(payload)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(65536)).get("error", str(exc))
            except (ValueError, OSError):
                error = str(exc)
            raise SharedRuntimeError(error) from exc
        except (OSError, ValueError) as exc:
            raise SharedRuntimeError("Shared runtime coordinator is unavailable: " + str(exc)) from exc

    def connect(self, *, start_if_missing=True):
        with self.start_lock:
            try:
                # A bind-only owned listener may need a short refusal window
                # before Winsock reports the normal ConnectionRefusedError.
                # Keep this first probe bounded, while the post-bootstrap
                # readiness polls below remain short and responsive.
                result = self._request("/v1/capabilities", timeout=5)
            except SharedRuntimeError as error:
                # Start only after a refused connection, never for a protocol mismatch,
                # timeout, hostile listener or unknown response on the control port.
                refused = isinstance(error.__cause__, URLError) and isinstance(error.__cause__.reason, ConnectionRefusedError)
                if not start_if_missing or not self.autostart or not refused or self.url != DEFAULT_URL:
                    raise
                self._start_coordinator()
                deadline = time.monotonic() + 20
                while True:
                    try:
                        result = self._request("/v1/capabilities", timeout=2)
                        break
                    except SharedRuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
            if result.get("service") != "aoitalk-shared-local-runtime" or result.get("protocol_version") != PROTOCOL_VERSION:
                raise SharedRuntimeError("Incompatible service on the shared runtime control port")
            with self.lock:
                if self.broker_id is not None and self.broker_id != result["broker_id"]:
                    self._reconcile_ended_epoch(result["broker_id"])
                self.broker_id = result["broker_id"]
        return result

    def _retire_local(self, ticket):
        """Retire only a released/expired lease, never interrupt an HTTP request."""
        with self.lock:
            self.release_pending.discard(ticket.ticket_id)
            ticket._released = True
            self.tickets.pop(ticket.ticket_id, None)
            if ticket._holder is not None:
                tickets = getattr(ticket._holder, ticket._holder_attribute, None)
                if isinstance(tickets, set):
                    tickets.discard(ticket)
                ticket._holder = None

    def _reconcile_ended_epoch(self, new_broker_id):
        with self.lock:
            # A queued release means the caller has already ended that request.
            for identifier in list(self.release_pending):
                ticket = self.tickets.get(identifier)
                if ticket is not None:
                    self._retire_local(ticket)
            if any(ticket.kind == "generation" and not ticket._released for ticket in self.tickets.values()):
                raise SharedRuntimeError("Coordinator restarted while a generation is active; end the request before reconnecting")
            for ticket in list(self.tickets.values()):
                self._retire_local(ticket)
            self.cancel_pending.clear()  # Acquisition is fenced by its old broker_id.
            self.broker_id = new_broker_id

    def _start_coordinator(self):
        from .launcher import launch_coordinator
        process = launch_coordinator()
        # Reap only the launcher handle if it exits; do not wait for/kill its tree.
        threading.Thread(target=process.wait, daemon=True).start()

    @staticmethod
    def _handle_cancel_requested(cancel_handle):
        event = getattr(cancel_handle, "cancel_requested", None)
        return bool(event is not None and event.is_set())

    def _request_pending_cancel(self, request_id, broker_id):
        """Mark one pending acquire cancelled and send its epoch-scoped stop."""
        thread = None
        cancel_request = None
        with self.lock:
            state = self._pending_acquires.get(request_id)
            if (
                state is None
                or state["broker_id"] != broker_id
                or self.pending.get(request_id) is not state["data"]
                or self.broker_id != broker_id
                or state["succeeded"]
            ):
                return
            state["cancel_requested"] = True
            if not state["cancel_started"]:
                state["cancel_started"] = True
                cancel_request = (
                    self.client_id,
                    self.pid,
                    broker_id,
                    request_id,
                )
                thread = threading.Thread(
                    target=self._cancel_pending_request,
                    args=(cancel_request,),
                    name="shared-runtime-acquire-cancel",
                    daemon=True,
                )
        if thread is not None:
            thread.start()

    def _cancel_pending_request(self, cancel_request):
        client_id, client_pid, broker_id, request_id = cancel_request
        self._cancel(
            request_id,
            broker_id=broker_id,
            client_id=client_id,
            client_pid=client_pid,
        )

    def _cancel_acquire_if_needed(self, request_id, broker_id):
        with self.lock:
            state = self._pending_acquires.get(request_id)
            if state is not None and state["cancel_started"]:
                return
        self._cancel(request_id, broker_id=broker_id)

    def acquire(self, profile_id, *, kind="consumer", snapshot_id=None, holder=None, force_restart=False, wait_for_model_ready=True, request_id=None, cancel_handle=None):
        if self.closed.is_set():
            raise SharedRuntimeError("Shared runtime client is closed")
        if cancel_handle is not None and self._handle_cancel_requested(cancel_handle):
            raise SharedRuntimeError("Shared runtime acquisition cancelled before launch")
        self.connect()
        if cancel_handle is not None and self._handle_cancel_requested(cancel_handle):
            raise SharedRuntimeError("Shared runtime acquisition cancelled before launch")
        data = {"client_id": self.client_id, "client_pid": self.pid, "broker_id": self.broker_id,
                "request_id": request_id or str(uuid.uuid4()), "profile_id": profile_id,
                "kind": kind, "force_restart": force_restart, "wait_for_model_ready": wait_for_model_ready}
        if snapshot_id:
            data["snapshot_id"] = snapshot_id
        remove_cancel_callback = None
        with self.lock:
            if self.closed.is_set():
                raise SharedRuntimeError("Shared runtime client is closed")
            if cancel_handle is not None and self._handle_cancel_requested(cancel_handle):
                raise SharedRuntimeError("Shared runtime acquisition cancelled before launch")
            self.pending[data["request_id"]] = data
            self._pending_acquires[data["request_id"]] = {
                "data": data,
                "broker_id": data["broker_id"],
                "cancel_requested": False,
                "cancel_started": False,
                "succeeded": False,
            }
        try:
            if cancel_handle is not None:
                remove_cancel_callback = cancel_handle.add_cancel_callback(
                    lambda: self._request_pending_cancel(
                        data["request_id"], data["broker_id"]
                    )
                )
            reply = self._request("/v1/acquire", data, timeout=3660)
            if reply.get("broker_id") != self.broker_id:
                raise SharedRuntimeError("Coordinator changed during acquisition")
            with self.lock:
                state = self._pending_acquires.get(data["request_id"])
                cancelled = (
                    self.closed.is_set()
                    or state is None
                    or state["cancel_requested"]
                    or self.broker_id != data["broker_id"]
                )
                if not cancelled:
                    ticket = self.tickets.get(reply["ticket"])
                    if ticket is None:
                        ticket = RemoteTicket(self, reply, kind, holder, snapshot_id=snapshot_id)
                        ticket.request_id = data["request_id"]
                        self.tickets[ticket.ticket_id] = ticket
                    if self._heartbeat_thread is None:
                        self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
                        self._heartbeat_thread.start()
                    state["succeeded"] = True
            if cancelled:
                self._cancel_acquire_if_needed(data["request_id"], data["broker_id"])
                raise SharedRuntimeError("Shared runtime acquisition cancelled")
        except BaseException:
            self._cancel_acquire_if_needed(data["request_id"], data["broker_id"])
            raise
        finally:
            if remove_cancel_callback is not None:
                remove_cancel_callback()
            with self.lock:
                self.pending.pop(data["request_id"], None)
                self._pending_acquires.pop(data["request_id"], None)
        return ticket, reply

    def _cancel(self, request_id, *, broker_id=None, client_id=None, client_pid=None):
        cancel_broker_id = self.broker_id if broker_id is None else broker_id
        cancel_client_id = self.client_id if client_id is None else client_id
        cancel_client_pid = self.pid if client_pid is None else client_pid
        with self.lock:
            state = self._pending_acquires.get(request_id)
            if state is not None and not state["succeeded"]:
                state["cancel_requested"] = True
                state["cancel_started"] = True
        try:
            self._request("/v1/cancel", {"client_id": cancel_client_id, "client_pid": cancel_client_pid,
                          "broker_id": cancel_broker_id, "request_id": request_id}, timeout=10)
        except SharedRuntimeError:
            with self.lock:
                if cancel_broker_id != self.broker_id:
                    log.debug(
                        "Dropping cancellation retry for ended broker epoch %s",
                        cancel_broker_id,
                    )
                    return False
                log.warning("Could not confirm acquisition cancellation; retaining request ID for retry")
                self.cancel_pending.add(request_id)
                if self._cancel_worker is None:
                    self._cancel_worker = threading.Thread(target=self._retry_cancellations, daemon=True)
                    self._cancel_worker.start()
            return False
        with self.lock:
            self.cancel_pending.discard(request_id)
        return True

    def cancel_request(self, request_id, *, broker_id):
        """Cancel exactly one of this client's acquisitions in one broker incarnation.

        A pending acquire is interrupted and a lease it already produced is released at the
        broker (which then stops a runtime nobody else holds); that lease is retired locally
        too, so no heartbeat keeps naming a ticket the broker no longer knows. Nothing else
        is touched. True only once the broker confirmed; an unconfirmed cancel stays queued
        for the existing retry worker."""
        confirmed = self._cancel(request_id, broker_id=broker_id)
        if confirmed:
            with self.lock:
                stale = [ticket for ticket in self.tickets.values()
                         if ticket.request_id == request_id and ticket.broker_id == broker_id]
            for ticket in stale:
                self._retire_local(ticket)
        return confirmed

    def stop_runtime(self, profile_id, *, expect_pid, expect_created, expect_launch_fingerprint):
        """Existing managed stop, fenced to one runtime incarnation.

        The broker refuses unless the single runtime of this profile is exactly that process
        (pid, create_time) with that launch fingerprint, so a restarted, replaced or foreign
        runtime is never stopped by a caller that verified someone else's process."""
        return self._request("/v1/stop", {
            "client_id": self.client_id, "client_pid": self.pid, "broker_id": self.broker_id,
            "profile_id": profile_id, "expect_pid": expect_pid, "expect_created": expect_created,
            "expect_launch_fingerprint": expect_launch_fingerprint}, timeout=70)

    def _retry_cancellations(self):
        # A failed network cancellation remains owned work, including after close.
        # The thread is daemonized; process death is independently reaped by the broker.
        while True:
            time.sleep(5)
            with self.lock:
                pending = list(self.cancel_pending)
                releases = [self.tickets[key] for key in self.release_pending if key in self.tickets]
                if not pending and not releases:
                    self._cancel_worker = None
                    return
            try:
                capabilities = self._request("/v1/capabilities", timeout=5)
                if capabilities.get("service") != "aoitalk-shared-local-runtime" or capabilities.get("protocol_version") != PROTOCOL_VERSION:
                    continue
                if capabilities.get("broker_id") != self.broker_id:
                    try:
                        self._reconcile_ended_epoch(capabilities["broker_id"])
                    except SharedRuntimeError:
                        # Other active generations must still explicitly finish.
                        pass
                    continue
            except SharedRuntimeError:
                continue
            for request_id in pending:
                self._cancel(request_id)
            for ticket in releases:
                try:
                    self.release(ticket)
                except SharedRuntimeError:
                    pass

    def validate(self, profile_id, snapshot_id, force_restart=False):
        self.connect()
        return self._request("/v1/validate", {"client_id": self.client_id, "client_pid": self.pid,
                             "profile_id": profile_id, "snapshot_id": snapshot_id, "force_restart": force_restart})

    def verify_ticket(self, ticket):
        """Verify one locally owned lease against the current broker epoch.

        Heartbeat is the native lease protocol's ownership probe: the
        coordinator rejects an unknown, expired, or foreign ticket with 409.
        Retire only that locally owned ticket on that definitive response;
        transport failures remain ambiguous and must not silently reacquire.
        """

        with self.lock:
            if self.closed.is_set() or ticket._released or self.tickets.get(ticket.ticket_id) is not ticket:
                return False
        self.connect(start_if_missing=False)
        with self.lock:
            if self.closed.is_set() or ticket._released or self.tickets.get(ticket.ticket_id) is not ticket:
                return False
            if ticket.broker_id != self.broker_id:
                self._retire_local(ticket)
                return False
            broker_id = self.broker_id
        try:
            self._request(
                "/v1/heartbeat",
                {
                    "client_id": self.client_id,
                    "broker_id": broker_id,
                    "tickets": [ticket.ticket_id],
                },
                timeout=5,
            )
        except SharedRuntimeError as error:
            if isinstance(error.__cause__, HTTPError) and error.__cause__.code == 409:
                self._retire_local(ticket)
                return False
            raise
        return True

    def _heartbeat(self):
        while not self.closed.wait(10):
            with self.lock:
                identifiers = list(self.tickets)
            if not identifiers:
                continue
            try:
                self._request("/v1/heartbeat", {"client_id": self.client_id, "broker_id": self.broker_id, "tickets": identifiers}, timeout=5)
            except SharedRuntimeError:
                # Keep leases locally. Never replace the model or silently reacquire
                # against a restarted coordinator during an active generation.
                log.warning("Shared runtime heartbeat failed", exc_info=True)

    def release(self, ticket):
        with self.lock:
            if ticket._released:
                return
        try:
            self._request("/v1/release", {"client_id": self.client_id, "broker_id": ticket.broker_id, "ticket": ticket.ticket_id}, timeout=10)
        except SharedRuntimeError:
            # release() is the caller's explicit end-of-request boundary. A
            # verified new broker cannot own this old-epoch ticket; retire it
            # locally instead of requiring the whole application to restart.
            try:
                current = self._request("/v1/capabilities", timeout=3)
                if (current.get("service") == "aoitalk-shared-local-runtime"
                        and current.get("protocol_version") == PROTOCOL_VERSION
                        and current.get("broker_id") != ticket.broker_id):
                    self._retire_local(ticket)
                    return
            except SharedRuntimeError:
                pass
            with self.lock:
                self.release_pending.add(ticket.ticket_id)
                if self._cancel_worker is None:
                    self._cancel_worker = threading.Thread(target=self._retry_cancellations, daemon=True)
                    self._cancel_worker.start()
            raise
        self._retire_local(ticket)

    def status(self):
        # Read-only probes never auto-start a coordinator or any worker.
        return self._request("/v1/status", timeout=3)

    def close(self):
        if self.closed.is_set():
            return
        with self.lock:
            self.closed.set()
            pending = list(self.pending)
            tickets = list(self.tickets.values())
        for request_id in pending:
            self._cancel(request_id)
        for ticket in tickets:
            try:
                self.release(ticket)
            except SharedRuntimeError:
                log.warning("Shared lease release failed; the coordinator will check process liveness")


class RemoteProcessStatus(SimpleNamespace):
    """Read-only ownership descriptor, never a tracked or killable Popen."""
    def poll(self):
        try:
            for row in self._client.status()["runtimes"]:
                if row.get("pid") == self.pid and row.get("launch_fingerprint") == self._fingerprint and row.get("running"):
                    return None
        except SharedRuntimeError:
            pass
        return 1
