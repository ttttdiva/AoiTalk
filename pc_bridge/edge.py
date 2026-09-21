"""Local IPC client for the user's Microsoft Edge extension.

No browser process/profile is created here. Edge owns the native host and its
existing cookies/tabs. The named pipe is local to this checkout and OS user.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
from multiprocessing.connection import Client
from pathlib import Path
from .paths import home
from typing import Any


def pipe_address(root: Path | None = None) -> str:
    root = root or home()
    identity = str(root.resolve()).casefold() + "|" + getpass.getuser()
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return rf"\\.\pipe\aoitalk-edge-{suffix}"


class EdgeBridgeError(RuntimeError):
    """Actionable error; the isolated-browser fallback intentionally does not exist."""


class EdgeBrowserBridge:
    def __init__(self, *, address: str | None = None, timeout: float = 35) -> None:
        self.address = address or pipe_address()
        self.timeout = timeout

    def _request(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt":
            raise EdgeBridgeError("edge_bridge_requires_windows")
        try:
            connection = Client(self.address, family="AF_PIPE")
        except (OSError, EOFError):
            raise EdgeBridgeError("edge_extension_not_connected") from None
        with connection:
            try:
                connection.send_bytes(
                    json.dumps({"command": command, "params": params}).encode()
                )
                if not connection.poll(self.timeout):
                    raise EdgeBridgeError("edge_command_timeout")
                result = json.loads(connection.recv_bytes(1_048_576))
            except (OSError, EOFError, ValueError):
                raise EdgeBridgeError("edge_connection_lost") from None
        if not isinstance(result, dict):
            raise EdgeBridgeError("edge_response_invalid")
        if not result.get("ok"):
            raise EdgeBridgeError(str(result.get("error") or "edge_command_failed"))
        value = result.get("result", {})
        if not isinstance(value, dict):
            raise EdgeBridgeError("edge_response_invalid")
        return value

    async def request(self, command: str, **params: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, command, params)

    @staticmethod
    def available() -> bool:
        # Windows exposes named pipes through the filesystem namespace. This
        # nonblocking probe does not launch Edge or a native host.
        return os.name == "nt" and os.path.exists(pipe_address())
