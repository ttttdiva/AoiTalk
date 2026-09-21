"""Request/response relay to explicitly selected, owner-bound remote PCs.

A connected PC dials out to FastAPI. Tools may execute on another event loop;
all WebSocket work is marshalled back to the loop owning that connection.
No command is replayed on reconnect and no server-local fallback exists.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .pc_bridge_store import PcBridgeError, PcBridgeStore


@dataclass
class Connection:
    owner: str
    device: str
    socket: Any
    loop: asyncio.AbstractEventLoop
    capabilities: list[str]
    edge_connected: bool
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task_lock: threading.Lock = field(default_factory=threading.Lock)
    connected_at: float = field(default_factory=time.time)


class PcBridgeHub:
    def __init__(self, store=None):
        self.store = store or PcBridgeStore()
        self.connections: dict[str, Connection] = {}
        self.lock = threading.RLock()

    def attach(self, owner, device, socket, hello):
        connection = Connection(owner, device, socket, asyncio.get_running_loop(),
                                hello.get('capabilities', []), bool(hello.get('edge_connected')))
        with self.lock:
            if device in self.connections:
                raise PcBridgeError('bridge_already_connected')
            self.connections[device] = connection
        return connection

    def detach(self, connection):
        with self.lock:
            if self.connections.get(connection.device) is connection:
                self.connections.pop(connection.device)
        for future in list(connection.pending.values()):
            if not future.done():
                future.set_exception(PcBridgeError('bridge_disconnected'))
        connection.pending.clear()

    def receive(self, connection, message):
        if message.get('type') == 'heartbeat':
            connection.edge_connected = bool(message.get('edge_connected'))
        elif message.get('type') == 'result':
            future = connection.pending.get(message.get('id'))
            if future and not future.done():
                future.set_result(message)

    def connection(self, owner: str, device: str) -> Connection:
        with self.lock:
            connection = self.connections.get(device)
        if not connection or connection.owner != owner:
            raise PcBridgeError('bridge_device_offline')
        return connection

    async def devices(self, owner: str):
        data = await self.store.list(owner)
        for device in data['devices']:
            with self.lock:
                connection = self.connections.get(device['id'])
            device.update(online=bool(connection and connection.owner == owner),
                          edge_connected=bool(connection and connection.owner == owner and connection.edge_connected),
                          capabilities=connection.capabilities if connection and connection.owner == owner else [])
        return data

    async def _on_loop(self, connection, coroutine):
        if asyncio.get_running_loop() is connection.loop:
            return await coroutine
        future = asyncio.run_coroutine_threadsafe(coroutine, connection.loop)
        return await asyncio.wrap_future(future)

    async def call(self, owner, device, channel, action, params=None, timeout=45):
        connection = self.connection(owner, device)
        if channel == 'computer' and action == 'type':
            text = (params or {}).get('text', '')
            if isinstance(text, str):
                timeout = max(timeout, min(540, 10 + len(text.encode('utf-16-le')) * 0.012))
        if channel not in connection.capabilities:
            raise PcBridgeError('bridge_capability_unavailable')
        return await self._on_loop(connection, self._call(connection, channel, action, params or {}, timeout))

    async def _call(self, connection, channel, action, params, timeout):
        async with connection.command_lock:
            # Re-check after acquiring: a reconnect must not inherit queued work.
            if self.connection(connection.owner, connection.device) is not connection:
                raise PcBridgeError('bridge_disconnected')
            request_id = uuid4().hex
            future = connection.loop.create_future()
            connection.pending[request_id] = future
            try:
                await connection.socket.send_json({
                    'type': 'command', 'id': request_id, 'channel': channel,
                    'action': action, 'params': params, 'expires_at': time.time() + timeout,
                })
                message = await asyncio.wait_for(future, timeout)
                if not message.get('ok'):
                    raise PcBridgeError(str(message.get('error') or 'bridge_command_failed'))
                result = message.get('result')
                if not isinstance(result, dict):
                    raise PcBridgeError('bridge_invalid_response')
                return result
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Delivery outcome may be unknown: never retry an input/click.
                await connection.socket.close(code=1011)
                raise
            finally:
                connection.pending.pop(request_id, None)

    async def revoke(self, owner, device):
        await self.store.revoke(owner, device)
        with self.lock:
            connection = self.connections.get(device)
        if connection and connection.owner == owner:
            await self._on_loop(connection, connection.socket.close(code=4003))


_HUB = PcBridgeHub()


def get_pc_bridge_hub():
    return _HUB


class RemoteControlBridge:
    def __init__(self, owner: str, device: str, hub=None):
        self.owner = owner
        self.device_id = device
        self.hub = hub or get_pc_bridge_hub()
        self.operation_lock = self.hub.connection(owner, device).task_lock

    async def request(self, command, **params):
        # Existing browser executor uses this interface; transport is remote.
        from pc_bridge.edge import EdgeBridgeError
        try:
            return await self.hub.call(self.owner, self.device_id, 'browser', command, params)
        except PcBridgeError as exc:
            raise EdgeBridgeError(str(exc)) from None

    async def computer(self, action, **params):
        return await self.hub.call(self.owner, self.device_id, 'computer', action, params)


async def resolve_control_bridge(device_id: str | None = None):
    from .turn_context import get_turn_context
    turn = get_turn_context()
    if not turn.user_id:
        raise PcBridgeError('bridge_authenticated_user_required')
    hub = get_pc_bridge_hub()
    selected = await hub.store.resolve(str(turn.user_id), device_id, turn.session_id)
    return RemoteControlBridge(str(turn.user_id), selected, hub)


def decode_screenshot(result: dict) -> bytes:
    image = result.get('screenshot') or {}
    if image.get('mime_type') != 'image/jpeg':
        raise PcBridgeError('bridge_screenshot_unavailable')
    try:
        return base64.b64decode(image['data'], validate=True)
    except (KeyError, ValueError):
        raise PcBridgeError('bridge_invalid_screenshot') from None
