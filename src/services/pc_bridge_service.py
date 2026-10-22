"""Request/response relay to explicitly selected, owner-bound remote PCs.

A connected PC dials out to FastAPI. Tools may execute on another event loop;
all WebSocket work is marshalled back to the loop owning that connection.
No command is replayed on reconnect and no server-local fallback exists.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .pc_bridge_store import PcBridgeError, PcBridgeStore
from pc_bridge.action_protocol import MAX_FILE_CHUNK_BYTES, sha256_json, validate_file_ref
from pc_bridge.browser_commands import GENERAL_BROWSER_COMMANDS, MEDIA_PREPARATION_COMMANDS
from pc_bridge.browser_commands import DEDICATED_BROWSER_COMMANDS, DEDICATED_BROWSER_PREFIX


@dataclass
class Connection:
    owner: str
    device: str
    socket: Any
    loop: asyncio.AbstractEventLoop
    capabilities: list[str]
    edge_connected: bool
    browser_backend: str = 'extension'
    browser_commands: tuple[str, ...] = ()
    media_preparation_backend: str | None = None
    media_preparation_commands: tuple[str, ...] = ()
    media_preparation_connected: bool = False
    machine_id: str | None = None
    boot_id: str | None = None
    apps_actions: tuple[str, ...] = ()
    apps_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    dedicated_browser_commands: tuple[str, ...] = ()
    dedicated_material_state_guard: bool = False
    epoch: str = field(default_factory=lambda: uuid4().hex)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    jev_relay: Any = None
    relay_tasks: set[asyncio.Task] = field(default_factory=set)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task_lock: threading.Lock = field(default_factory=threading.Lock)
    connected_at: float = field(default_factory=time.time)


class PcBridgeHub:
    def __init__(self, store=None, *, dedicated_enabled=None):
        self.store = store or PcBridgeStore()
        self.connections: dict[str, Connection] = {}
        self.lock = threading.RLock()
        self.dedicated_enabled = dedicated_enabled or (lambda: False)

    def attach(self, owner, device, socket, hello):
        backend = hello.get('browser_backend', 'extension')
        if backend not in {'extension', 'agent_browser', 'managed_profile'}:
            raise PcBridgeError('bridge_browser_backend_invalid')
        media_backend = hello.get('media_preparation_backend')
        if media_backend not in {None, 'managed_profile'}:
            raise PcBridgeError('bridge_browser_backend_invalid')

        def commands(key, allowed, default=()):
            values = hello.get(key, default)
            if (not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or v not in allowed for v in values)
                    or len(set(values)) != len(values)):
                raise PcBridgeError('bridge_browser_commands_invalid')
            return tuple(values)

        general = commands('browser_commands', GENERAL_BROWSER_COMMANDS,
                           sorted(GENERAL_BROWSER_COMMANDS) if backend == 'agent_browser' else ())
        media = commands('media_preparation_commands', MEDIA_PREPARATION_COMMANDS)
        dedicated = commands('dedicated_browser_commands', DEDICATED_BROWSER_COMMANDS)
        capabilities = list(hello.get('capabilities', []))
        machine_id = hello.get('machine_id')
        boot_id = hello.get('boot_id')
        if 'apps' in capabilities:
            if (not isinstance(machine_id, str) or not machine_id or len(machine_id) > 128
                    or not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128):
                raise PcBridgeError('bridge_apps_identity_invalid')
        apps_actions = hello.get('apps_actions', ())
        if (not isinstance(apps_actions, (list, tuple)) or len(apps_actions) > 256
                or any(not isinstance(item, str) or not item or len(item) > 128 for item in apps_actions)
                or len(set(apps_actions)) != len(apps_actions)):
            raise PcBridgeError('bridge_apps_catalog_invalid')
        raw_catalog = hello.get('apps_catalog', ())
        if not isinstance(raw_catalog, (list, tuple)) or len(raw_catalog) > 256:
            raise PcBridgeError('bridge_apps_catalog_invalid')
        if 'apps' in capabilities and apps_actions and not raw_catalog:
            raise PcBridgeError('bridge_apps_catalog_invalid')
        apps_catalog: dict[str, dict[str, Any]] = {}
        for item in raw_catalog:
            if (not isinstance(item, Mapping) or not isinstance(item.get('saved_action_id'), str)
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                    or not isinstance(item.get('contract_hash'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', item['contract_hash'])
                    or item.get('mode') not in {'finite', 'service', 'desktop'}):
                raise PcBridgeError('bridge_apps_catalog_invalid')
            key = item['saved_action_id']
            if key in apps_catalog or (apps_actions and key not in apps_actions):
                raise PcBridgeError('bridge_apps_catalog_invalid')
            dependencies = item.get('dependencies', ())
            if (not isinstance(dependencies, (list, tuple)) or len(dependencies) > 64
                    or any(not isinstance(dep, Mapping) or set(dep) != {'sha256'}
                           or not isinstance(dep.get('sha256'), str)
                           or not re.fullmatch(r'[0-9a-f]{64}', dep['sha256'])
                           for dep in dependencies)):
                raise PcBridgeError('bridge_apps_catalog_invalid')
            fingerprint = item.get('contract_fingerprint')
            if fingerprint is not None and (not isinstance(fingerprint, str)
                                            or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
                raise PcBridgeError('bridge_apps_catalog_invalid')
            apps_catalog[key] = {name: item.get(name) for name in (
                'saved_action_id', 'sha256', 'contract_hash', 'mode', 'dependencies', 'contract_fingerprint')}
        # A legacy media-only profile never provides generic browser commands.
        if backend == 'managed_profile':
            capabilities = [c for c in capabilities if c != 'browser']
        connection = Connection(owner, device, socket, asyncio.get_running_loop(),
                                capabilities, bool(hello.get('edge_connected')) and 'browser' in capabilities,
                                browser_backend=backend, browser_commands=general,
                                media_preparation_backend=media_backend, media_preparation_commands=media,
                                machine_id=machine_id if isinstance(machine_id, str) else None,
                                boot_id=boot_id if isinstance(boot_id, str) else None,
                                apps_actions=tuple(apps_actions),
                                apps_catalog=apps_catalog,
                                dedicated_browser_commands=dedicated,
                                dedicated_material_state_guard=hello.get('dedicated_material_state_guard') is True
                                and type(hello.get('dedicated_material_state_guard_version')) is int
                                and hello['dedicated_material_state_guard_version'] == 3
                                and 'dedicated_browser' in capabilities and bool(dedicated),
                                media_preparation_connected=bool(hello.get('media_preparation_connected')) and
                                media_backend == 'managed_profile' and 'media_prepare' in capabilities)
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
        connection.jev_relay = None
        for task in tuple(connection.relay_tasks):
            task.cancel()

    def receive(self, connection, message):
        with self.lock:
            if self.connections.get(connection.device) is not connection:
                return
        if message.get('type') == 'heartbeat':
            connection.edge_connected = bool(message.get('edge_connected')) and 'browser' in connection.capabilities
            connection.media_preparation_connected = bool(message.get('media_preparation_connected')) and \
                connection.media_preparation_backend == 'managed_profile' and 'media_prepare' in connection.capabilities
        elif message.get('type') == 'result':
            future = connection.pending.get(message.get('id'))
            if future and not future.done():
                future.set_result(message)
        elif message.get('type') in {'jev_ask', 'jev_gate', 'jev_type'}:
            binding = connection.jev_relay
            context = binding['context'].copy() if binding else copy_context()
            task = connection.loop.create_task(self._jev_reply(connection, message, binding), context=context)
            connection.relay_tasks.add(task)
            task.add_done_callback(connection.relay_tasks.discard)

    async def _jev_reply(self, connection, message, binding):
        """Callback replies never acquire command_lock held by the long RPC."""
        kind = message['type']
        reply = {'type': kind + '_reply', 'id': message.get('id'), 'run_id': message.get('run_id'),
                 'owner_user_id': connection.owner, 'connection_epoch': connection.epoch}
        error = 'bridge_jev_binding_invalid'
        try:
            identifier = message.get('id')
            if (binding is None or binding is not connection.jev_relay or
                    self.connection(connection.owner, connection.device) is not connection or
                    not self.dedicated_enabled() or
                    not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or
                    message.get('run_id') != binding['run_id'] or
                    message.get('owner_user_id') != connection.owner or
                    message.get('connection_epoch') != connection.epoch or
                    type(message.get('tab_id')) is not int or message.get('tab_id') != binding['tab_id'] or
                    message.get('browser_context') != binding['browser_context'] or
                    type(message.get('version')) is not int or message.get('version') != 1):
                raise PcBridgeError(error)
            if identifier in binding['seen'] or len(binding['seen']) >= 512:
                # An event ID has exactly one reply; duplicates are ignored.
                return
            binding['seen'].add(identifier)
            if binding['busy']:
                raise PcBridgeError('bridge_jev_event_overlap')
            binding['busy'] = True
            try:
                async def invoke():
                    return await binding['handler'](message)
                if asyncio.get_running_loop() is binding['loop']:
                    result = await invoke()
                else:
                    future = asyncio.run_coroutine_threadsafe(invoke(), binding['loop'])
                    result = await asyncio.wrap_future(future)
            finally:
                binding['busy'] = False
            if (connection.jev_relay is not binding or not self.dedicated_enabled() or
                    self.connection(connection.owner, connection.device) is not connection):
                raise PcBridgeError('bridge_jev_authority_revoked')
            reply.update(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, PcBridgeError) else 'bridge_jev_callback_failed'
            reply.update({'allow': False, 'reason': error} if kind == 'jev_gate' else {'ok': False, 'error': error})
        try:
            await connection.socket.send_json(reply)
        except Exception:
            self.detach(connection)

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
                          capabilities=connection.capabilities if connection and connection.owner == owner else [],
                          browser_backend=connection.browser_backend if connection and connection.owner == owner else None,
                          browser_commands=list(connection.browser_commands) if connection and connection.owner == owner else [],
                          media_preparation_backend=connection.media_preparation_backend if connection and connection.owner == owner else None,
                          media_preparation_connected=bool(connection and connection.owner == owner and connection.media_preparation_connected),
                          media_preparation_commands=list(connection.media_preparation_commands) if connection and connection.owner == owner else [],
                          apps=bool(connection and connection.owner == owner and 'apps' in connection.capabilities),
                          apps_actions=list(connection.apps_actions) if connection and connection.owner == owner else [],
                          machine_id=connection.machine_id if connection and connection.owner == owner else device.get('machine_id'))
        return data

    async def _on_loop(self, connection, coroutine):
        if asyncio.get_running_loop() is connection.loop:
            return await coroutine
        future = asyncio.run_coroutine_threadsafe(coroutine, connection.loop)
        return await asyncio.wrap_future(future)

    async def call(self, owner, device, channel, action, params=None, timeout=45, *, expected_connection=None,
                   dispatch_preflight=None, dispatch_gate=None, jev_relay=None):
        connection = self.connection(owner, device)
        if expected_connection is not None and connection is not expected_connection:
            raise PcBridgeError('bridge_connection_changed')
        if channel == 'computer' and action == 'type':
            text = (params or {}).get('text', '')
            if isinstance(text, str):
                timeout = max(timeout, min(540, 10 + len(text.encode('utf-16-le')) * 0.012))
        capability = channel
        if channel == 'apps':
            from pc_bridge.action_protocol import ActionProtocolError, validate_action_request
            try:
                validated = validate_action_request(action, params or {})
            except ActionProtocolError as exc:
                raise PcBridgeError(str(exc)) from None
            if 'apps' not in connection.capabilities:
                raise PcBridgeError('bridge_capability_unavailable')
            if (action == 'start' and connection.apps_actions
                    and validated['saved_action_id'] not in connection.apps_actions):
                raise PcBridgeError('bridge_saved_action_unavailable')
            if action == 'start' and connection.apps_catalog:
                catalog = connection.apps_catalog.get(validated['saved_action_id'])
                if (catalog is None
                        or catalog.get('sha256') != validated['sha256']
                        or catalog.get('contract_hash') != validated['contract_hash']):
                    raise PcBridgeError('bridge_saved_action_contract_mismatch')
        if channel == 'browser' and isinstance(action, str) and action.startswith(DEDICATED_BROWSER_PREFIX):
            if not self.dedicated_enabled() and not self._dedicated_cleanup(action):
                raise PcBridgeError('bridge_dedicated_disabled')
            capability = 'dedicated_browser'
            if action[len(DEDICATED_BROWSER_PREFIX):] not in connection.dedicated_browser_commands:
                raise PcBridgeError('bridge_browser_command_unavailable')
        elif channel == 'browser' and isinstance(action, str) and action.startswith('media_prepare_'):
            if connection.media_preparation_backend == 'managed_profile':
                capability = 'media_prepare'
                if action not in connection.media_preparation_commands:
                    raise PcBridgeError('bridge_browser_command_unavailable')
            elif connection.browser_backend != 'extension':
                raise PcBridgeError('bridge_capability_unavailable')
        elif channel == 'browser' and connection.browser_backend == 'agent_browser':
            if action not in connection.browser_commands:
                raise PcBridgeError('bridge_browser_command_unavailable')
        if capability not in connection.capabilities:
            raise PcBridgeError('bridge_capability_unavailable')
        if action == DEDICATED_BROWSER_PREFIX + 'jev_run' and jev_relay is None:
            raise PcBridgeError('bridge_jev_runtime_required')
        return await self._on_loop(connection, self._call(connection, channel, action, params or {}, timeout,
                                                        dispatch_preflight=dispatch_preflight, dispatch_gate=dispatch_gate,
                                                        jev_relay=jev_relay))

    async def _call(self, connection, channel, action, params, timeout, *, dispatch_preflight=None, dispatch_gate=None,
                    jev_relay=None):
        async with connection.command_lock:
            # Re-check after acquiring: a reconnect must not inherit queued work.
            if self.connection(connection.owner, connection.device) is not connection:
                raise PcBridgeError('bridge_disconnected')
            if channel == 'browser' and action.startswith(DEDICATED_BROWSER_PREFIX):
                enabled = self.dedicated_enabled()
                await connection.socket.send_json({'type': 'dedicated_browser_gate', 'enabled': enabled})
            # These callbacks are server-local authority, never wire params.
            # Run them after queue/gate awaits. A known preflight rejection has
            # sent no command and must not leave a possibly-dispatched fence.
            try:
                if dispatch_preflight is not None:
                    await dispatch_preflight()
                if self.connection(connection.owner, connection.device) is not connection:
                    raise PcBridgeError('bridge_disconnected')
                if (channel == 'browser' and action.startswith(DEDICATED_BROWSER_PREFIX) and
                        (not enabled or not self.dedicated_enabled()) and not self._dedicated_cleanup(action)):
                    raise PcBridgeError('bridge_dedicated_disabled')
                if dispatch_gate is not None:
                    dispatch_gate()
            except Exception:
                if dispatch_gate is None:
                    raise
                return {'ok': False, 'executed': False, 'execution_status': 'blocked',
                        'error': 'bridge_dispatch_preflight_rejected'}
            request_id = uuid4().hex
            future = connection.loop.create_future()
            connection.pending[request_id] = future
            if jev_relay is not None:
                connection.jev_relay = jev_relay
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
                if jev_relay is not None:
                    connection.jev_relay = None
                    tasks = tuple(connection.relay_tasks)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _dedicated_cleanup(action):
        return action in {DEDICATED_BROWSER_PREFIX + 'end_run', DEDICATED_BROWSER_PREFIX + 'cancel_login'}

    async def revoke(self, owner, device):
        await self.store.revoke(owner, device)
        with self.lock:
            connection = self.connections.get(device)
        if connection and connection.owner == owner:
            await self._on_loop(connection, connection.socket.close(code=4003))

    async def sync_dedicated_gate(self):
        enabled = self.dedicated_enabled()
        with self.lock:
            targets = [item for item in self.connections.values() if 'dedicated_browser' in item.capabilities]
        for connection in targets:
            try:
                if not enabled:
                    for task in tuple(connection.relay_tasks):
                        connection.loop.call_soon_threadsafe(task.cancel)
                await asyncio.wait_for(self._on_loop(connection, connection.socket.send_json(
                    {'type': 'dedicated_browser_gate', 'enabled': enabled})), 5)
            except Exception:
                # A peer which cannot receive revocation loses all authority.
                self.detach(connection)
                try:
                    await asyncio.wait_for(self._on_loop(connection, connection.socket.close(code=1011)), 5)
                except Exception:
                    pass


_HUB = PcBridgeHub()


def get_pc_bridge_hub():
    return _HUB


class RemoteControlBridge:
    supports_material_state_guard = False
    def __init__(self, owner: str, device: str, hub=None):
        self.owner = owner
        self.device_id = device
        self.hub = hub or get_pc_bridge_hub()
        self._connection = self.hub.connection(owner, device)
        self.operation_lock = self._connection.task_lock
        self.browser_backend = self._connection.browser_backend
        self.media_preparation_backend = self._connection.media_preparation_backend

    def _check_connection(self):
        # A task is bound to the authenticated connection epoch, not merely a
        # device ID that a fresh connection could reclaim mid-run.
        if self.hub.connection(self.owner, self.device_id) is not self._connection:
            raise PcBridgeError('bridge_connection_changed')

    @property
    def connection_epoch(self):
        self._check_connection()
        return self._connection.epoch

    async def request(self, command, *, dispatch_preflight=None, dispatch_gate=None, **params):
        # Existing browser executor uses this interface; transport is remote.
        from pc_bridge.edge import EdgeBridgeError
        try:
            self._check_connection()
            if ('expected_material_state' in params or 'expected_material_state_digest' in params) and not self.supports_material_state_guard:
                return {'ok': False, 'executed': False, 'error': 'material_state_changed', 'execution_status': 'blocked'}
            caller_loop = asyncio.get_running_loop()

            async def preflight_on_caller_loop():
                # DB sessions stay on the caller's loop even when this transport
                # marshals its socket work to a different owning loop.
                if asyncio.get_running_loop() is caller_loop:
                    await dispatch_preflight()
                else:
                    future = asyncio.run_coroutine_threadsafe(dispatch_preflight(), caller_loop)
                    await asyncio.wrap_future(future)

            return await self.hub.call(self.owner, self.device_id, 'browser', command, params,
                expected_connection=self._connection,
                dispatch_preflight=preflight_on_caller_loop if dispatch_preflight is not None else None,
                dispatch_gate=dispatch_gate)
        except PcBridgeError as exc:
            raise EdgeBridgeError(str(exc)) from None

    async def computer(self, action, **params):
        self._check_connection()
        return await self.hub.call(self.owner, self.device_id, 'computer', action, params, expected_connection=self._connection)


class RemoteAppsControlBridge(RemoteControlBridge):
    """Fixed saved-action transport bound to one authenticated WSS epoch."""

    async def request(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self._check_connection()
        return await self.hub.call(self.owner, self.device_id, 'apps', action, params,
                                   expected_connection=self._connection)

    async def start(self, **params):
        return await self.request('start', params)

    async def status(self, *, run_id):
        return await self.request('status', {'run_id': run_id})

    async def logs(self, *, run_id, cursor=0, limit=100):
        return await self.request('logs', {'run_id': run_id, 'cursor': cursor, 'limit': limit})

    async def stop(self, *, run_id):
        return await self.request('stop', {'run_id': run_id})

    async def upload(self, **params):
        return await self.request('upload', params)

    async def download(self, **params):
        return await self.request('download', params)


class PcBridgeAppActionTransport:
    """Adapter for ``AppActionRunService`` with a fixed apps wire protocol.

    The server-side immutable action record contains routing metadata such as
    execution target and probe.  Only the six approved start fields cross the
    WebSocket; the PC resolves mode/probe/path from its own catalog.
    """

    def __init__(self, hub=None):
        self.hub = hub or get_pc_bridge_hub()

    def _bridge(self, owner_user_id: str, device_id: str, machine_id: str, *,
                saved_action_id: str | None = None, sha256: str | None = None,
                contract_hash: str | None = None, mode: str | None = None):
        connection = self.hub.connection(str(owner_user_id), str(device_id))
        if connection.machine_id != str(machine_id):
            raise PcBridgeError('bridge_machine_id_mismatch')
        if 'apps' not in connection.capabilities:
            raise PcBridgeError('bridge_capability_unavailable')
        if saved_action_id is not None and connection.apps_catalog:
            catalog = connection.apps_catalog.get(str(saved_action_id))
            if (catalog is None
                    or (sha256 is not None and catalog.get('sha256') != str(sha256))
                    or (contract_hash is not None and catalog.get('contract_hash') != str(contract_hash))
                    or (mode is not None and catalog.get('mode') != str(mode))):
                raise PcBridgeError('bridge_saved_action_contract_mismatch')
        return RemoteAppsControlBridge(str(owner_user_id), str(device_id), self.hub)

    @staticmethod
    def _snapshot(value: Mapping[str, Any], *, action_ref: str, machine_id: str,
                  mode: str | None = None, run_id: str | None = None,
                  saved_action_id: str | None = None, sha256: str | None = None,
                  contract_hash: str | None = None,
                  input_sha256: str | None = None) -> dict[str, Any]:
        if (not isinstance(value, Mapping)
                or str(value.get('run_id') or '') != str(run_id or value.get('run_id') or '')
                or str(value.get('action_ref') or '') != str(action_ref)
                or str(value.get('machine_id') or '') != str(machine_id)):
            raise PcBridgeError('bridge_apps_identity_mismatch')
        for field, expected in (('saved_action_id', saved_action_id), ('sha256', sha256),
                                ('contract_hash', contract_hash)):
            if expected is not None and str(value.get(field) or '') != str(expected):
                raise PcBridgeError('bridge_apps_identity_mismatch')
        if mode is not None and value.get('mode') is not None and str(value.get('mode')) != str(mode):
            raise PcBridgeError('bridge_apps_identity_mismatch')
        if input_sha256 is not None:
            raw_input_hash = value.get('input_sha256')
            if raw_input_hash is None and value.get('input') is not None:
                try:
                    raw_input_hash = sha256_json(value.get('input'))
                except Exception:
                    raw_input_hash = None
            if str(raw_input_hash or '') != str(input_sha256):
                raise PcBridgeError('bridge_apps_identity_mismatch')
        mode = str(value.get('mode')) if value.get('mode') is not None else mode
        status = str(value.get('status') or value.get('execution_status') or 'unknown')
        phase = {'accepted': 'accepted', 'starting': 'starting', 'running': 'running',
                 'ready': 'running', 'stop_requested': 'stopping', 'stopped': 'cancelled',
                 'succeeded': 'succeeded', 'failed': 'failed', 'unknown': 'unknown'}.get(status, 'unknown')
        ready = value.get('ready') is True
        if mode == 'finite' or (mode is None and status == 'succeeded'):
            readiness = 'not_required'
        elif ready:
            readiness = 'ready'
        elif status == 'unknown':
            readiness = 'unknown'
        else:
            readiness = 'pending'
        pid = value.get('pid')
        process_status = 'running' if (type(pid) is int and pid > 0 and status not in {'succeeded', 'failed', 'stopped', 'unknown'}) else (
            'not_started' if pid is None and status in {'accepted', 'starting'} else 'exited')
        raw_result = value['result'] if 'result' in value else {}
        result = {"run_id": value.get('run_id'), "action_ref": value.get('action_ref'),
                  "machine_id": value.get('machine_id'), "phase": phase,
                  "execution_status": value.get('execution_status', status),
                  "readiness_status": readiness, "process_status": process_status,
                  "exit_code": value.get('exit_code'), "ready": ready,
                  "result": raw_result, "result_present": value.get('result_present') is True or 'result' in value,
                  "error": value.get('error')}
        for key in ('saved_action_id', 'sha256', 'contract_hash', 'input_sha256', 'connection_epoch', 'journal_epoch', 'remote_journal_epoch'):
            if value.get(key) is not None:
                result[key] = value[key]
        return result

    async def availability(self, *, owner_user_id, device_id, machine_id, execution_target_id=None, **_kwargs):
        try:
            connection = self.hub.connection(str(owner_user_id), str(device_id))
            online = 'apps' in connection.capabilities and connection.machine_id == str(machine_id)
            saved_action_id = _kwargs.get('saved_action_id')
            expected_mode = _kwargs.get('mode', _kwargs.get('execution_mode'))
            expected_sha = _kwargs.get('sha256')
            expected_contract = _kwargs.get('contract_hash')
            if not saved_action_id or expected_sha is None or expected_contract is None or expected_mode is None:
                return {"online": False, "device_id": str(device_id),
                        "machine_id": connection.machine_id,
                        "connection_epoch": connection.epoch,
                        "error_code": "app_action_saved_action_identity_missing",
                        "reason": "saved_action_id/sha256/contract_hash/mode が未指定です"}
            catalog_map = connection.apps_catalog if isinstance(connection.apps_catalog, Mapping) else {}
            catalog = catalog_map.get(str(saved_action_id))
            if not catalog_map or catalog is None:
                online = False
            else:
                for key, expected in (('sha256', expected_sha), ('contract_hash', expected_contract),
                                      ('mode', expected_mode)):
                    online = online and catalog.get(key) == str(expected)
            return {"online": online,
                    "device_id": str(device_id), "machine_id": connection.machine_id,
                    "connection_epoch": connection.epoch,
                    "error_code": None if online else "app_action_saved_action_unavailable",
                    "reason": None if online else "bridge_saved_action_catalog_unavailable"}
        except PcBridgeError:
            return {"online": False, "device_id": str(device_id), "machine_id": None}

    async def start(self, *, owner_user_id, device_id, machine_id, payload, **_kwargs):
        if not isinstance(payload, Mapping):
            raise PcBridgeError('apps_start_params_invalid')
        # Filter server bookkeeping fields deliberately.  The PC endpoint then
        # validates this exact fixed set again before catalog resolution.
        fixed_keys = {'saved_action_id', 'sha256', 'contract_hash', 'run_id', 'action_ref', 'input'}
        if not fixed_keys <= set(payload):
            raise PcBridgeError('apps_start_params_invalid')
        fixed = {key: payload[key] for key in fixed_keys}
        expected_mode = payload.get('execution_mode', payload.get('mode'))
        bridge = self._bridge(owner_user_id, device_id, machine_id,
                              saved_action_id=str(payload['saved_action_id']),
                              sha256=str(payload['sha256']),
                              contract_hash=str(payload['contract_hash']),
                              mode=str(expected_mode) if expected_mode is not None else None)
        result = await bridge.start(**fixed)
        return self._snapshot(result, action_ref=str(payload['action_ref']), machine_id=str(machine_id),
                              mode=str(expected_mode) if expected_mode is not None else None,
                              run_id=str(payload['run_id']),
                              saved_action_id=str(payload['saved_action_id']), sha256=str(payload['sha256']),
                              contract_hash=str(payload['contract_hash']),
                              input_sha256=sha256_json(payload['input']))

    async def stage_inputs(self, *, owner_user_id, device_id, machine_id, run_id, chunks, **_kwargs):
        """Upload verified input chunks before the PC Action journal start fence."""
        bridge = self._bridge(owner_user_id, device_id, machine_id)
        for chunk in chunks:
            raw = chunk.get("data") if isinstance(chunk, Mapping) else None
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise PcBridgeError("bridge_apps_input_chunk_invalid")
            raw_bytes = bytes(raw)
            ref = str(chunk.get("ref") or "")
            offset = int(chunk.get("offset", 0))
            await bridge.upload(
                run_id=str(run_id),
                ref=ref,
                offset=offset,
                data=base64.b64encode(raw_bytes).decode("ascii"),
                chunk_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            )

    async def download_outputs(self, *, owner_user_id, device_id, machine_id, run_id, outputs, **_kwargs):
        """Download result file refs and return byte payloads for file capture."""
        bridge = self._bridge(owner_user_id, device_id, machine_id)
        result = []
        for item in outputs:
            if not isinstance(item, Mapping):
                continue
            raw_ref = (item.get("ref") or item.get("remote_ref") or item.get("path")
                       or item.get("source_ref") or "")
            if not raw_ref:
                continue
            try:
                ref = validate_file_ref(str(raw_ref).replace("\\", "/"))
            except Exception as exc:
                raise PcBridgeError("bridge_apps_output_ref_invalid") from exc
            offset = 0
            chunks = bytearray()
            expected_hash = str(item.get("sha256") or "").lower() or None
            expected_size = int(item.get("size_bytes")) if item.get("size_bytes") is not None else None
            while True:
                page = await bridge.download(run_id=str(run_id), ref=ref, offset=offset, limit=MAX_FILE_CHUNK_BYTES)
                data = base64.b64decode(str(page.get("data") or ""), validate=True)
                chunks.extend(data)
                offset = int(page.get("next_offset", offset + len(data)))
                if page.get("eof") is True or not data:
                    break
            payload = bytes(chunks)
            if expected_size is not None and len(payload) != expected_size:
                raise PcBridgeError("bridge_apps_output_size_mismatch")
            if expected_hash and hashlib.sha256(payload).hexdigest() != expected_hash:
                raise PcBridgeError("bridge_apps_output_hash_mismatch")
            copied = dict(item)
            copied["data"] = payload
            copied["remote_ref"] = ref
            copied["ref"] = ref
            copied["source_ref"] = str(item.get("source_ref") or ref)
            copied.setdefault("filename", Path(ref).name)
            result.append(copied)
        return result

    async def status(self, *, owner_user_id, device_id, machine_id, run_id, action_ref, **_kwargs):
        bridge = self._bridge(owner_user_id, device_id, machine_id)
        result = await bridge.status(run_id=str(run_id))
        return self._snapshot(result, action_ref=str(action_ref), machine_id=str(machine_id), run_id=str(run_id),
                              input_sha256=_kwargs.get('input_sha256'))

    async def logs(self, *, owner_user_id, device_id, machine_id, run_id, action_ref,
                   cursor=0, limit_bytes=64 * 1024, **_kwargs):
        bridge = self._bridge(owner_user_id, device_id, machine_id)
        # The wire limit is a bounded entry count; the adapter caps it by a
        # conservative byte budget so server projections remain bounded.
        limit = max(1, min(200, int(limit_bytes) // 256 or 1))
        raw = await bridge.logs(run_id=str(run_id), cursor=max(0, int(cursor)), limit=limit)
        if not isinstance(raw, Mapping) or str(raw.get('run_id') or '') != str(run_id):
            raise PcBridgeError('bridge_apps_identity_mismatch')
        nested = raw.get('run')
        if not isinstance(nested, Mapping):
            raise PcBridgeError('bridge_apps_identity_mismatch')
        snapshot = self._snapshot(
            nested,
            action_ref=str(action_ref), machine_id=str(machine_id), run_id=str(run_id),
            mode=_kwargs.get('mode', _kwargs.get('execution_mode')),
            saved_action_id=_kwargs.get('saved_action_id'), sha256=_kwargs.get('sha256'),
            contract_hash=_kwargs.get('contract_hash'), input_sha256=_kwargs.get('input_sha256'),
        )
        entries = raw.get('entries', [])
        if not isinstance(entries, list):
            raise PcBridgeError('bridge_apps_logs_invalid')
        typed_entries = []
        for entry in entries:
            if not isinstance(entry, Mapping) or type(entry.get('sequence')) is not int \
                    or not isinstance(entry.get('stream'), str) or not isinstance(entry.get('line'), str):
                raise PcBridgeError('bridge_apps_logs_invalid')
            typed_entries.append({
                'sequence': entry['sequence'], 'stream': entry['stream'],
                'line': entry['line'], 'created_at': entry.get('created_at'),
            })
        return {
            'run_id': str(run_id), 'cursor': int(raw.get('cursor', cursor)),
            'entries': typed_entries,
            'next_cursor': int(raw.get('next_cursor', cursor)),
            'latest_cursor': int(raw.get('latest_cursor', raw.get('next_cursor', cursor))),
            'run': snapshot,
        }

    async def stop(self, *, owner_user_id, device_id, machine_id, run_id, action_ref, **_kwargs):
        bridge = self._bridge(owner_user_id, device_id, machine_id)
        result = await bridge.stop(run_id=str(run_id))
        return self._snapshot(result, action_ref=str(action_ref), machine_id=str(machine_id), run_id=str(run_id),
                              input_sha256=_kwargs.get('input_sha256'))


def get_pc_bridge_app_action_transport() -> PcBridgeAppActionTransport:
    """Return the shared owner/device/epoch-bound Action transport."""
    return PcBridgeAppActionTransport(get_pc_bridge_hub())


class RemoteDedicatedControlBridge(RemoteControlBridge):
    """Same selected PC and epoch; only the separately gated owned profile."""
    browser_backend = 'owned_profile'

    def __init__(self, owner, device, hub=None, *, cleanup_only=False):
        super().__init__(owner, device, hub)
        self.browser_backend = 'owned_profile'
        self.cleanup_only = cleanup_only

    @property
    def supports_material_state_guard(self):
        self._check_connection()
        return self._connection.dedicated_material_state_guard

    async def request(self, command, **params):
        if (command not in DEDICATED_BROWSER_COMMANDS or
                (self.cleanup_only and command not in {'end_run', 'cancel_login'})):
            from pc_bridge.edge import EdgeBridgeError
            raise EdgeBridgeError('profile_driver_command_rejected')
        return await super().request(DEDICATED_BROWSER_PREFIX + command, **params)

    async def jev_run(self, *, handler, browser_context, timeout, **params):
        self._check_connection()
        if self.cleanup_only:
            raise PcBridgeError('bridge_jev_runtime_required')
        binding = {'handler': handler, 'loop': asyncio.get_running_loop(), 'context': copy_context(),
                   'run_id': params['run_id'], 'tab_id': params['tab_id'],
                   'browser_context': browser_context, 'seen': set(), 'busy': False}
        return await self.hub.call(self.owner, self.device_id, 'browser', DEDICATED_BROWSER_PREFIX + 'jev_run',
            params, timeout=timeout, expected_connection=self._connection, jev_relay=binding)


async def resolve_control_bridge(device_id: str | None = None, *, dedicated=False, cleanup_only=False):
    from .turn_context import get_turn_context
    turn = get_turn_context()
    if not turn.user_id:
        raise PcBridgeError('bridge_authenticated_user_required')
    hub = get_pc_bridge_hub()
    selected = await hub.store.resolve(str(turn.user_id), device_id, turn.session_id)
    if dedicated:
        if not cleanup_only and not hub.dedicated_enabled():
            raise PcBridgeError('bridge_dedicated_disabled')
        return RemoteDedicatedControlBridge(str(turn.user_id), selected, hub, cleanup_only=cleanup_only)
    if cleanup_only:
        raise PcBridgeError('bridge_cleanup_target_required')
    return RemoteControlBridge(str(turn.user_id), selected, hub)


def decode_screenshot(result: dict) -> bytes:
    image = result.get('screenshot') or {}
    if image.get('mime_type') != 'image/jpeg':
        raise PcBridgeError('bridge_screenshot_unavailable')
    try:
        return base64.b64decode(image['data'], validate=True)
    except (KeyError, ValueError):
        raise PcBridgeError('bridge_invalid_screenshot') from None
