"""One reader for a verified owned child; no daemon or process creation."""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque

from .codex_app_profiles import CodexProfileError


class OwnedStdioRouter:
    def __init__(self, session):
        self.session, self.transport = session, session.transport
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._serial, self._prefix = 0, uuid.uuid4().hex
        self._pending, self._server = {}, {}
        self._retired = deque(maxlen=128)
        self.task = None
        self.control = OwnedStdioChannel(self, task=False)
        self.transport.set_control(self.check_wire)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def check_wire(self):
        self.session.check()
        self.control.check_control()
        task = self.task
        if task is not None:
            task.check_control()

    def fail(self):
        self._stop.set()
        self.session.service._fail_connection(self.session.record, "codex_profile_owned_stdio_failed")

    def close(self):
        self._stop.set()
        # The session closes its owned process before joining this reader.
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=1)

    def open_task(self, lease, check, workspace):
        lease.check(owner=lease.owner, read_roots=[workspace])
        if lease.session is not self.session:
            raise CodexProfileError("codex_profile_connection_invalidated")
        with self._lock:
            if self._stop.is_set() or self.task is not None:
                raise CodexProfileError("codex_profile_owned_transport_busy")
            value = OwnedStdioChannel(self, task=True, check=check, workspace=workspace)
            self.task = value
            self.session.model_contract_verified = False
            return value

    def send(self, channel, message):
        channel.check_control()
        wire = dict(message)
        with self._lock:
            if self._stop.is_set() or (channel.is_task and self.task is not channel):
                raise CodexProfileError("codex_profile_owned_transport_closed")
            method, request_id = message.get("method"), message.get("id")
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                raise CodexProfileError("codex_profile_owned_rpc_id_required")
            if method:
                permitted = {"configRequirements/read", "config/read", "account/read"}
                if channel.is_task:
                    permitted |= {"model/list", "thread/start", "turn/start", "turn/interrupt", "thread/unsubscribe"}
                if method not in permitted or len(self._pending) >= 32:
                    raise CodexProfileError("codex_profile_owned_rpc_forbidden")
                params = message.get("params") or {}
                if method in {"turn/start", "turn/interrupt", "thread/unsubscribe"} and params.get("threadId") != channel.thread_id:
                    raise CodexProfileError("codex_profile_owned_thread_mismatch")
                if method == "thread/start":
                    if channel.thread_id or channel.binding:
                        raise CodexProfileError("codex_profile_owned_thread_reuse")
                    channel.binding = True
                self._serial += 1
                wire_id = f"aoitalk-owned-{self._prefix}-{self._serial}"
                wire["id"] = wire_id
                self._pending[wire_id] = (channel, request_id, method)
            else:
                if not channel.is_task or self._server.get(request_id) is not channel:
                    raise CodexProfileError("codex_profile_owned_tool_response_unmatched")
                del self._server[request_id]
        try:
            self.transport.send(wire)
        except Exception:
            self.fail()
            raise

    def _read(self):
        while not self._stop.is_set():
            try:
                self.session.check()
                try:
                    message = self.transport.receive(timeout=0.05)
                except queue.Empty:
                    continue
                with self._lock:
                    self._dispatch(message)
            except Exception:
                if not self._stop.is_set():
                    self.fail()
                return

    def _dispatch(self, message):
        if not isinstance(message, dict):
            raise CodexProfileError("codex_profile_invalid_rpc_message")
        if "id" in message and "method" not in message:
            target = self._pending.pop(message["id"], None)
            if target is None or ("result" in message) == ("error" in message):
                raise CodexProfileError("codex_profile_owned_rpc_unmatched")
            channel, original, method = target
            if method == "thread/start" and "result" in message:
                result = message["result"]
                thread = result.get("thread") if isinstance(result, dict) else None
                thread_id = thread.get("id") if isinstance(thread, dict) else None
                if not isinstance(thread_id, str) or not thread_id or thread_id in self._retired:
                    raise CodexProfileError("codex_profile_owned_thread_unverified")
                channel.thread_id, channel.binding = thread_id, False
            channel.offer({**message, "id": original})
            if method == "thread/start":
                for held in channel.held:
                    self._task_event(channel, held)
                channel.held.clear()
            return
        method = message.get("method")
        if method in {"account/updated", "account/rateLimits/updated", "account/login/completed"}:
            self.session._notification(message)
            if method == "account/updated":
                self.session.model_contract_verified = False
            if self.task is not None:
                self.task.offer(message)
            return
        params = message.get("params") or {}
        if not isinstance(params, dict) or not isinstance(method, str):
            raise CodexProfileError("codex_profile_unexpected_notification")
        thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        if method == "thread/closed" and thread_id in self._retired:
            return
        channel = self.task
        if channel is None:
            raise CodexProfileError("codex_profile_owned_event_without_task")
        if channel.binding:
            if len(channel.held) >= 128:
                raise CodexProfileError("codex_profile_owned_queue_full")
            channel.held.append(message)
            return
        self._task_event(channel, message)

    def _task_event(self, channel, message):
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        if channel.thread_id and thread_id not in {None, channel.thread_id}:
            raise CodexProfileError("codex_profile_owned_thread_mismatch")
        if "id" in message:
            request_id = message["id"]
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool) or request_id in channel.server_ids or len(self._server) >= 32 or len(channel.server_ids) >= 4096:
                raise CodexProfileError("codex_profile_owned_server_id_unverified")
            channel.server_ids.add(request_id)
            self._server[request_id] = channel
        channel.offer(message)

    def release_task(self, channel):
        with self._lock:
            if (self.task is not channel or any(value[0] is channel for value in self._pending.values())
                    or channel in self._server.values()):
                raise CodexProfileError("codex_profile_owned_task_pending")
            self._retired.append(channel.thread_id)
            self.task = None
            channel.closed = True

    def drain_or_release(self, channel):
        # Empty observation and retirement share the reader's dispatch lock.
        # A late event is handled on this task or fails closed after retirement.
        with self._lock:
            try:
                return channel.messages.get_nowait()
            except queue.Empty:
                self.release_task(channel)
                return None


class OwnedStdioChannel:
    def __init__(self, router, *, task, check=None, workspace=None):
        self.router, self.is_task, self.workspace = router, task, workspace
        self._control = check or router.session.check
        self._rpc_lock = threading.RLock()
        self.messages = queue.Queue(maxsize=128)
        self.thread_id, self.binding, self.closed = None, False, False
        self.held = []
        self.server_ids = set()
        self._serial = 0

    def check_control(self):
        if self.closed or self.router._stop.is_set():
            raise CodexProfileError("codex_profile_owned_transport_closed")
        self._control()

    def set_control(self, callback):
        self._control = callback

    def offer(self, value):
        try:
            self.messages.put_nowait(value)
        except queue.Full:
            raise CodexProfileError("codex_profile_owned_queue_full") from None

    def send(self, value):
        self.router.send(self, value)

    def receive(self, timeout):
        self.check_control()
        return self.messages.get(timeout=timeout)

    def rpc(self, method, params, *, handler=None, timeout=20):
        with self._rpc_lock:
            self._serial += 1
            request_id = f"channel-{self._serial}"
            self.send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    value = self.receive(0.05)
                except queue.Empty:
                    continue
                if value.get("id") == request_id and "method" not in value:
                    if "error" in value or not isinstance(value.get("result"), dict):
                        raise CodexProfileError("codex_profile_owned_rpc_failed")
                    return value["result"]
                if handler is None:
                    raise CodexProfileError("codex_profile_owned_unexpected_control_event")
                handler(value)
            raise CodexProfileError("codex_profile_owned_rpc_timeout")

    def verify_model_contract(self):
        self.check_control()
        if not self.is_task or not self.thread_id:
            raise CodexProfileError("codex_profile_owned_thread_unverified")
        self.router.session.model_contract_verified = True

    def finish(self, *, completed, handler):
        if self.closed:
            return
        try:
            if not completed or not self.thread_id:
                raise CodexProfileError("codex_profile_owned_task_aborted")
            result = self.rpc("thread/unsubscribe", {"threadId": self.thread_id}, handler=handler, timeout=2)
            if result.get("status") != "unsubscribed":
                raise CodexProfileError("codex_profile_owned_unsubscribe_unverified")
            while True:
                self.check_control()
                value = self.router.drain_or_release(self)
                if value is None:
                    break
                handler(value)
        except Exception:
            self.router.fail()
        finally:
            self.closed = True
