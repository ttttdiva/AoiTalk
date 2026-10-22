"""WebSocket RPC over the official daemon's byte-only stdio proxy.

The Sans-I/O protocol performs no network connection. Process ownership,
bounded writes and cleanup remain in the shared owned process transport.
"""
from __future__ import annotations

import json
import queue
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from websockets.client import ClientProtocol
from websockets.frames import Frame, Opcode
from websockets.http11 import Response
from websockets.protocol import State
from websockets.uri import parse_uri

from .codex_task_completion import CodexTaskProtocolError, MAX_MESSAGE_CHARS, _ProcessTransport


class CodexProxyWebSocketTransport:
    def __init__(self, command: list[str], cwd: Path, *, process_factory=_ProcessTransport, profile=None):
        self._protocol = ClientProtocol(parse_uri("ws://localhost/"), max_size=MAX_MESSAGE_CHARS)
        self._process = process_factory(command, cwd, raw_bytes=True, profile=profile) if profile is not None else process_factory(command, cwd, raw_bytes=True)
        self._control: Callable[[], Any] = lambda: None
        self._frames: deque[Frame] = deque()
        self._fragments = bytearray()
        self._ready = False
        self._closed = False

    def set_control(self, callback: Callable[[], Any]) -> None:
        self._control = callback
        self._process.set_control(callback)

    def _check(self) -> None:
        self._control()
        if self._closed:
            raise CodexTaskProtocolError("codex_proxy_closed")

    def _flush(self) -> None:
        for data in self._protocol.data_to_send():
            if data:
                self._process.send_bytes(data)

    def _feed(self, timeout: float) -> None:
        self._check()
        data = self._process.receive(timeout=timeout)
        if not isinstance(data, bytes) or not data:
            raise CodexTaskProtocolError("codex_proxy_invalid_byte_stream")
        self._protocol.receive_data(data)
        if self._protocol.handshake_exc is not None:
            raise CodexTaskProtocolError("codex_proxy_invalid_handshake")
        if self._protocol.parser_exc is not None:
            raise CodexTaskProtocolError("codex_proxy_invalid_frame")
        for event in self._protocol.events_received():
            if isinstance(event, Response):
                self._ready = True
            elif isinstance(event, Frame):
                self._frames.append(event)
        self._flush()  # Sans-I/O automatically handles ping and close replies.

    def _handshake(self) -> None:
        if self._ready:
            return
        self._check()
        self._protocol.send_request(self._protocol.connect())
        self._flush()
        deadline = time.monotonic() + 5.0
        while not self._ready:
            self._check()
            if time.monotonic() >= deadline:
                raise CodexTaskProtocolError("codex_proxy_handshake_timeout")
            try:
                self._feed(0.05)
            except queue.Empty:
                continue

    def send(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        # Reserve the maximum client-frame header for the bounded byte writer.
        if len(encoded) > MAX_MESSAGE_CHARS - 14:
            raise CodexTaskProtocolError("codex_proxy_message_too_large")
        self._handshake()
        self._check()
        if self._protocol.state != State.OPEN:
            raise CodexTaskProtocolError("codex_proxy_daemon_closed")
        self._protocol.send_text(encoded)
        self._flush()

    def receive(self, timeout: float) -> dict[str, Any]:
        self._handshake()
        deadline = time.monotonic() + timeout
        while True:
            self._check()
            if not self._frames:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._feed(min(remaining, 0.05))
                continue
            frame = self._frames.popleft()
            if frame.opcode == Opcode.CLOSE:
                raise CodexTaskProtocolError("codex_proxy_daemon_closed")
            if frame.opcode in (Opcode.PING, Opcode.PONG):
                continue
            if frame.opcode not in (Opcode.TEXT, Opcode.CONT):
                raise CodexTaskProtocolError("codex_proxy_invalid_message_type")
            self._fragments.extend(frame.data)
            if not frame.fin:
                continue
            payload = bytes(self._fragments)
            self._fragments.clear()
            try:
                value = json.loads(payload.decode("utf-8"),
                                   parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError) as exc:
                raise CodexTaskProtocolError("codex_proxy_invalid_json") from exc
            if not isinstance(value, dict):
                raise CodexTaskProtocolError("codex_proxy_invalid_json")
            return value

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._process.close()
