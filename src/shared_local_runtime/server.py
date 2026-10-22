"""Versioned local HTTP control plane. Model traffic stays on the model endpoint."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

from . import LEASE_TTL_SECONDS, MAX_BODY_BYTES, PROTOCOL_VERSION
from . import registry

log = logging.getLogger(__name__)


class Conflict(RuntimeError):
    pass


def _uuid(value, label):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid " + label) from exc


class Coordinator:
    def __init__(self, backend, *, registry_root=registry.ROOT, clock=time.monotonic):
        self.backend = backend
        self.registry_root = registry_root
        self.clock = clock
        self.broker_id = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.lifecycle = threading.RLock()
        self.clients = {}
        self.tickets = {}
        self.requests = {}
        self.cancelled = set()
        self.runtimes = {}
        self.pending = {}

    def capabilities(self):
        return {"service": "aoitalk-shared-local-runtime", "protocol_version": PROTOCOL_VERSION,
                "broker_id": self.broker_id, "ttl_seconds": LEASE_TTL_SECONDS,
                "capabilities": ["consumer-leases", "generation-leases", "launch-fingerprint", "safe-unload"]}

    def _client(self, data, *, register=False):
        client_id = _uuid(data.get("client_id"), "client_id")
        if not register and data.get("broker_id") != self.broker_id:
            raise Conflict("Stale broker_id; the previous lease is no longer valid")
        with self.lock:
            client = self.clients.get(client_id)
            if register:
                pid = data.get("client_pid")
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    raise ValueError("client_pid is required")
                try:
                    created = psutil.Process(pid).create_time()
                except psutil.Error as exc:
                    raise ValueError("Client process is not running") from exc
                if client and (client["pid"], client["created"]) != (pid, created):
                    raise Conflict("client_id belongs to another process incarnation")
                if client is None:
                    client = {"pid": pid, "created": created}
                    self.clients[client_id] = client
            if client is None:
                raise Conflict("Unknown client_id")
            client["heartbeat"] = self.clock()
        return client_id

    def _client_still_alive(self, identifier):
        client = self.clients.get(identifier)
        if client is None:
            return False
        try:
            process = psutil.Process(client["pid"])
            return process.create_time() == client["created"] and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    @contextmanager
    def _acquisition_lifecycle(self, client, key):
        # A queued cancellation must not wait for another client's cold load.
        while not self.lifecycle.acquire(timeout=0.1):
            with self.lock:
                if key in self.cancelled or not self._client_still_alive(client):
                    raise Conflict("Acquisition was cancelled")
        try:
            yield
        finally:
            self.lifecycle.release()

    def acquire(self, data):
        # Fence the handshake-to-acquire restart race before allocating anything.
        if data.get("broker_id") != self.broker_id:
            raise Conflict("Stale broker_id; repeat discovery before acquisition")
        client = self._client(data, register=True)
        request = _uuid(data.get("request_id"), "request_id")
        kind = data.get("kind")
        if kind not in {"generation", "consumer"}:
            raise ValueError("kind must be consumer or generation")
        model = data.get("profile_id")
        key = (client, request)
        signature = (model, data.get("snapshot_id"), kind)
        # Serializes cold starts and switches; releases never acquire this lock.
        with self._acquisition_lifecycle(client, key):
            with self.lock:
                if key in self.cancelled or not self._client_still_alive(client):
                    raise Conflict("Acquisition was cancelled")
                previous = self.requests.get(key)
                if previous:
                    if previous["signature"] != signature:
                        raise Conflict("request_id was already used for another request")
                    if previous["ticket"] not in self.tickets:
                        raise Conflict("request_id belongs to a released lease")
                    return dict(previous["reply"])
            cancel_event = threading.Event()
            with self.lock:
                self.pending[key] = {"profile_id": model, "kind": kind, "cancel_event": cancel_event}
                if key in self.cancelled:
                    cancel_event.set()
            try:
                return self._acquire_pending(data, client, key, signature, cancel_event)
            finally:
                with self.lock:
                    self.pending.pop(key, None)

    def _acquire_pending(self, data, client, key, signature, cancel_event):
        model = data.get("profile_id")
        kind = data["kind"]
        config, snapshot_id = registry.read(model, data.get("snapshot_id"), root=self.registry_root)
        target = self.backend.prepare(config, model)
        endpoint = target["base_url"]
        with self.lock:
            current = self.runtimes.get(endpoint)
            active = [t for t in self.tickets.values() if t["target"]["base_url"] == endpoint]
            changed = current and current["launch_fingerprint"] != target["launch_fingerprint"]
            if active and (changed or data.get("force_restart")):
                raise Conflict("The endpoint is leased with a different launch configuration or an active generation")
        if cancel_event.is_set():
            raise Conflict("Acquisition was cancelled")
        if current and changed:
            self.backend.stop(current)
        started = self.backend.ensure(target, force_restart=bool(data.get("force_restart")),
                                      wait_for_model_ready=bool(data.get("wait_for_model_ready", True)),
                                      cancel_event=cancel_event)
        # Generation begin must verify model readiness, even for an already running route.
        with self.lock:
            cancelled = key in self.cancelled or not self._client_still_alive(client)
        if cancelled:
            if started:
                self.backend.stop(target)
            raise Conflict("Acquisition was cancelled")
        try:
            lease = self.backend.acquire(target, kind, cancel_event=cancel_event)
        except BaseException:
            # A newly started worker belongs to this acquisition. Reused
            # workers and other consumers retain their existing ownership.
            if started:
                self.backend.stop(target)
            raise
        ticket = str(uuid.uuid4())
        reply = {key: value for key, value in target.items() if key in {"profile_id", "base_url", "model", "launch_fingerprint", "runtime"}}
        reply.update(ticket=ticket, broker_id=self.broker_id, ttl_seconds=LEASE_TTL_SECONDS, started=bool(started))
        try:
            registry.publish(model, snapshot_id, runtime=target["runtime"], root=self.registry_root)
        except Exception:
            lease.release()
            if started:
                self.backend.stop(target)
            raise
        with self.lock:
            if key in self.cancelled or not self._client_still_alive(client):
                lease.release()
                if started:
                    self.backend.stop(target)
                raise Conflict("Acquisition was cancelled")
            self.runtimes[endpoint] = target
            self.tickets[ticket] = {"client": client, "target": target, "kind": kind, "lease": lease, "seen": self.clock()}
            self.requests[key] = {"signature": signature, "ticket": ticket, "reply": reply}
        return reply

    def cancel(self, data):
        client = self._client(data, register=True)
        if data.get("broker_id") != self.broker_id:
            raise Conflict("Stale broker_id")
        key = (client, _uuid(data.get("request_id"), "request_id"))
        with self.lock:
            self.cancelled.add(key)
            pending = self.pending.get(key)
            if pending is not None:
                pending["cancel_event"].set()
            previous = self.requests.get(key)
        if previous:
            self._release(client, previous["ticket"])
        return {"cancelled": True}

    def validate(self, data):
        self._client(data, register=True)
        config, _ = registry.read(data.get("profile_id"), data.get("snapshot_id"), root=self.registry_root)
        target = self.backend.prepare(config, data["profile_id"])
        # Preflight checks settings/artifacts, not transition availability. The
        # subsequent acquire atomically enforces leases after the caller releases
        # its old main-route retention; other sessions remain protected.
        return {"ok": True, "launch_fingerprint": target["launch_fingerprint"]}

    def heartbeat(self, data):
        client = self._client(data)
        tickets = data.get("tickets")
        if not isinstance(tickets, list) or len(tickets) > 1024:
            raise ValueError("tickets must be a bounded list")
        with self.lock:
            for identifier in tickets:
                entry = self.tickets.get(identifier)
                if entry is None or entry["client"] != client:
                    raise Conflict("Unknown or foreign ticket")
            for identifier in tickets:
                self.tickets[identifier]["seen"] = self.clock()
        return {"ok": True}

    def _release(self, client, identifier):
        with self.lock:
            entry = self.tickets.get(identifier)
            if entry is None:
                return False
            if entry["client"] != client:
                raise Conflict("Cannot release another client's ticket")
            self.tickets.pop(identifier)
        entry["lease"].release()
        # Stop outside the request thread so release can drain a waiting transition.
        threading.Thread(target=self._stop_if_idle, args=(entry["target"],), daemon=True).start()
        return True

    def release(self, data):
        client = self._client(data)
        return {"released": self._release(client, _uuid(data.get("ticket"), "ticket"))}

    def _stop_if_idle(self, target):
        with self.lifecycle:
            with self.lock:
                if any(t["target"]["base_url"] == target["base_url"] for t in self.tickets.values()):
                    return
                current = self.runtimes.get(target["base_url"])
                if current is None or current["launch_fingerprint"] != target["launch_fingerprint"]:
                    return
            try:
                self.backend.stop(target)
                with self.lock:
                    if not self.backend.status(target).get("running"):
                        self.runtimes.pop(target["base_url"], None)
            except Exception:
                log.exception("Idle shared runtime cleanup failed")

    def stop(self, data):
        if data.get("broker_id") != self.broker_id:
            raise Conflict("Stale broker_id")
        self._client(data, register="client_pid" in data)
        model = data.get("profile_id")
        with self.lock:
            if any(not model or p["profile_id"] == model for p in self.pending.values()):
                raise Conflict("Runtime startup is still owned; cancel its acquisition first")
            targets = [t for t in self.runtimes.values() if not model or t["profile_id"] == model]
            if any(t["target"] in targets for t in self.tickets.values()):
                raise Conflict("Runtime is still leased; release its consumers and generations first")
        for target in targets:
            self._stop_if_idle(target)
        return {"stopped": len(targets)}

    def reap(self):
        """Heartbeat expiry prompts a liveness check, never revokes a live generation.

        A stalled event loop/GPU request can outlast a heartbeat. Only a verified dead
        PID incarnation loses its leases. Unknown process status fails closed.
        """
        with self.lock:
            clients = list(self.clients.items())
        for identifier, client in clients:
            if self.clock() - client["heartbeat"] < LEASE_TTL_SECONDS:
                continue
            try:
                process = psutil.Process(client["pid"])
                dead = process.create_time() != client["created"] or process.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                dead = True
            except psutil.Error:
                dead = False
            if dead:
                with self.lock:
                    for key, pending in self.pending.items():
                        if key[0] == identifier:
                            self.cancelled.add(key)
                            pending["cancel_event"].set()
                    tickets = [ticket for ticket, entry in self.tickets.items() if entry["client"] == identifier]
                for ticket in tickets:
                    self._release(identifier, ticket)
                with self.lock:
                    self.clients.pop(identifier, None)
                    self.cancelled = {key for key in self.cancelled if key[0] != identifier}
                    for key in list(self.requests):
                        if key[0] == identifier:
                            self.requests.pop(key)

    def status(self):
        with self.lock:
            pending = [
                {"profile_id": item["profile_id"], "kind": item["kind"],
                 "cancel_requested": item["cancel_event"].is_set()}
                for item in self.pending.values()
            ]
            runtimes = []
            for target in self.runtimes.values():
                item = {k: target[k] for k in ("profile_id", "base_url", "model", "runtime", "launch_fingerprint")}
                item.update(self.backend.status(target))
                own = [t for t in self.tickets.values() if t["target"]["base_url"] == target["base_url"]]
                item.update(consumers=sum(t["kind"] == "consumer" for t in own), generations=sum(t["kind"] == "generation" for t in own))
                runtimes.append(item)
        profiles = []
        for path in sorted((self.registry_root / "profiles").glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                profiles.append({"profile_id": record["model"], "runtime": record.get("runtime")})
            except (OSError, ValueError, KeyError):
                continue
        return {"broker_id": self.broker_id, "runtimes": runtimes, "profiles": profiles,
                "pending_acquisitions": pending}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _boundary(self):
        expected = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
        if self.headers.get("Host") not in expected or self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") is not None:
            self._reject_request(403, {"error": "Only native loopback clients are allowed"})
            return False
        return True

    def _reject_request(self, status, payload, *, size=None):
        # Windows may reset a socket closed with an unread upload, hiding the
        # HTTP error, including for small bodies rejected by the Host/Origin
        # boundary. Discard only declared bytes, capped at one above our limit.
        # Unknown/chunked framing is never parsed. An absolute deadline also
        # bounds repeated small writes. No rejection reaches JSON/backend code.
        self.close_connection = True
        if size is None:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
        remaining = min(max(size, 0), MAX_BODY_BYTES + 1)
        if self.headers.get("Transfer-Encoding") or getattr(self, "_request_body_consumed", False):
            remaining = 0
        self._request_body_consumed = True
        if remaining:
            deadline = time.monotonic() + 5
            previous_timeout = self.connection.gettimeout()
            try:
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)
        self._send(status, payload)

    def do_GET(self):
        if not self._boundary():
            return
        if self.path == "/v1/capabilities":
            self._send(200, self.server.coordinator.capabilities())
        elif self.path == "/v1/status":
            self._send(200, self.server.coordinator.status())
        else:
            self._reject_request(404, {"error": "Unknown route"})

    def do_POST(self):
        if not self._boundary():
            return
        routes = {"/v1/acquire": "acquire", "/v1/cancel": "cancel", "/v1/validate": "validate", "/v1/heartbeat": "heartbeat", "/v1/release": "release", "/v1/stop": "stop"}
        action = routes.get(self.path)
        if action is None:
            self._reject_request(404, {"error": "Unknown route"})
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._reject_request(415, {"error": "A bounded application/json body is required"})
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if size < 0 or size > MAX_BODY_BYTES:
                self._reject_request(413, {"error": "Request body is too large or unbounded"}, size=size)
                return
            raw = self.rfile.read(size)
            self._request_body_consumed = True
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            allowed = {"client_id", "client_pid", "request_id", "profile_id", "kind", "broker_id", "ticket", "tickets", "snapshot_id", "force_restart", "wait_for_model_ready"}
            if set(data) - allowed:
                raise ValueError("Unknown control fields")
            self._send(200, getattr(self.server.coordinator, action)(data))
        except Conflict as exc:
            self._send(409, {"error": str(exc)})
        except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            log.exception("Shared lifecycle request failed")
            self._send(503, {"error": str(exc)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, coordinator):
        if address[0] != "127.0.0.1":
            raise ValueError("Coordinator may only bind IPv4 loopback")
        self.coordinator = coordinator
        super().__init__(address, Handler)
