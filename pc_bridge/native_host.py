"""Edge Native Messaging host. stdlib only; no secrets or browser profiles read."""

from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
from multiprocessing.connection import Listener
from uuid import uuid4

from .edge import pipe_address
from .paths import home

MAX_MESSAGE = 1_048_576


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            if not data:
                return None
            raise EOFError("truncated native message")
        data.extend(chunk)
    return bytes(data)


def read_message(stream):
    header = read_exact(stream, 4)
    if header is None:
        return None
    length = struct.unpack("<I", header)[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("native message size")
    body = read_exact(stream, length)
    if body is None:
        raise EOFError("truncated native message")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("native message must be an object")
    return value


def write_message(stream, message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE:
        raise ValueError("native message size")
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


def main():
    if os.name != "nt":
        raise SystemExit("Microsoft Edge native host requires Windows")
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    replies = {}
    lock = threading.Lock()
    output_lock = threading.Lock()
    stopped = threading.Event()
    # The native messaging port keeps this host alive. A second connected
    # profile gets a clear error instead of silently switching the target.
    try:
        listener = Listener(pipe_address(home()), family="AF_PIPE")
    except OSError:
        write_message(
            sys.stdout.buffer,
            {
                "event": "host_error",
                "error": "別のEdgeプロファイルが接続中です。そちらの拡張で切断してください。",
            },
        )
        return

    def handle(connection):
        request_id = uuid4().hex
        reply = queue.Queue(maxsize=1)
        try:
            command = json.loads(connection.recv_bytes(MAX_MESSAGE))
            with lock:
                replies[request_id] = reply
            with output_lock:
                write_message(sys.stdout.buffer, {"id": request_id, **command})
            try:
                response = reply.get(timeout=32)
            except queue.Empty:
                response = {"ok": False, "error": "edge_command_timeout"}
            connection.send_bytes(json.dumps(response, ensure_ascii=False).encode())
        except (OSError, EOFError, ValueError):
            pass
        finally:
            with lock:
                replies.pop(request_id, None)
            connection.close()

    def accept():
        while not stopped.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                break
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        with output_lock:
            write_message(sys.stdout.buffer, {"event": "connected"})
        while True:
            message = read_message(sys.stdin.buffer)
            if message is None:
                break
            with lock:
                reply = replies.get(message.get("id"))
            if reply is not None:
                try:
                    reply.put_nowait(message)
                except queue.Full:
                    pass
    except (OSError, EOFError, ValueError):
        pass
    finally:
        stopped.set()
        with lock:
            for reply in replies.values():
                try:
                    reply.put_nowait({"ok": False, "error": "edge_connection_lost"})
                except queue.Full:
                    pass
        listener.close()


if __name__ == "__main__":
    main()
