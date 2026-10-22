"""Outbound-only WebSocket client with explicit GUI lifecycle and no replay."""
from __future__ import annotations
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import VERSION
from .browser_backend import create_browser_backend
from .browser_backend import load_helper_bundle
from .browser_commands import GENERAL_BROWSER_COMMANDS, MEDIA_PREPARATION_COMMANDS
from .browser_commands import DEDICATED_BROWSER_COMMANDS, DEDICATED_BROWSER_PREFIX
from .dedicated_browser_backend import DedicatedBrowserBridge
from .profile_browser_driver import dedicated_runtime_available
from .managed_profile_backend import ManagedProfileBrowserBridge
from .purpose_browser_bridge import PurposeBrowserBridge
from .media_asset_transfer import MediaAssetTransferStore
from .action_identity import boot_id as current_boot_id
from .action_identity import stable_machine_id
from .action_protocol import ActionProtocolError
from .action_runner import ActionRunner


def websocket_url(server):
    url = urlsplit(str(server).strip())
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('サーバーURLは http(s)://host:port の形式で指定してください')
    return urlunsplit(('wss' if url.scheme == 'https' else 'ws', url.netloc,
                      url.path.rstrip('/') + '/api/pc-bridge/connect', '', ''))


class BridgeClient:
    def __init__(self, config, status=lambda value: None):
        if config.get('browser_backend', 'agent_browser') not in {'agent_browser', 'managed_profile', 'extension'}:
            raise ValueError('browser_backend_unknown')
        self.config = config
        self.status = status
        self.stopped = threading.Event()
        self.operation_cancelled = threading.Event()
        self.websocket = None
        self.browser_backend = 'agent_browser'
        self.media_preparation_backend = 'managed_profile'
        # Local old mode names never select an extension or act as fallback.
        self.edge = PurposeBrowserBridge(
            create_browser_backend({**config, 'browser_backend': 'agent_browser'}, cancel_event=self.operation_cancelled),
            ManagedProfileBrowserBridge(cancel_event=self.operation_cancelled))
        self.desktop = None
        self.dedicated = DedicatedBrowserBridge(cancel_event=self.operation_cancelled)
        self.media_asset_store = None
        try:
            self.action_runner = ActionRunner(config)
        except (ActionProtocolError, OSError, ValueError):
            # Invalid local catalog entries never become a remote capability.
            # Normal browser/computer bridge operation remains available so a
            # user can repair the explicitly maintained catalog.
            self.action_runner = None
        self.machine_id = (self.action_runner.machine_id if self.action_runner is not None
                           else config.get('machine_id') or stable_machine_id())
        self.boot_id = current_boot_id()
        self._dispatch_lock = threading.Lock()
        # jev_run's judgment/gate relay: events from the owned driver are sent
        # over the authenticated connection and answered by id; never replayed.
        self._jev_waiters = {}
        self._jev_lock = threading.Lock()
        self._jev_identity = None
        self.dedicated.set_jev_handler(self._jev_relay)

    def _jev_relay(self, kind, run_id, payload):
        if (kind not in {'jev_ask', 'jev_gate', 'jev_type'} or self.stopped.is_set()
                or self.operation_cancelled.is_set()):
            raise RuntimeError('bridge_jev_relay_unavailable')
        socket_, identity, context = self.websocket, self._jev_identity, self.dedicated.jev_context()
        if (socket_ is None or identity is None or context is None or not self.dedicated.available() or not isinstance(run_id, str)
                or context.get('run_id') != run_id):
            raise RuntimeError('bridge_jev_relay_unavailable')
        context = {**context, 'browser_context': dict(context['browser_context'])}
        event_id = uuid4().hex
        waiter = {'event': threading.Event(), 'reply': None,
                  'identity': (kind + '_reply', event_id, run_id, *identity)}
        with self._jev_lock:
            self._jev_waiters[event_id] = waiter
        message = {'type': kind, 'id': event_id, 'run_id': run_id, 'owner_user_id': identity[0],
                   'connection_epoch': identity[1], 'tab_id': context['tab_id'],
                   'browser_context': context['browser_context'], 'version': 1,
                   ('info' if kind == 'jev_gate' else 'request'): payload}
        try:
            socket_.send(json.dumps(message, ensure_ascii=False, allow_nan=False))
            deadline = time.monotonic() + (650 if kind == 'jev_gate' else 75)
            while not waiter['event'].wait(timeout=0.1):
                if (self.stopped.is_set() or self.operation_cancelled.is_set()
                        or self.websocket is not socket_ or self._jev_identity != identity
                        or not self.dedicated.available() or self.dedicated.jev_context() != context):
                    raise RuntimeError('bridge_jev_reply_invalid')
                if time.monotonic() >= deadline:
                    raise TimeoutError('bridge_jev_relay_timeout')
            reply = waiter['reply']
        finally:
            with self._jev_lock:
                self._jev_waiters.pop(event_id, None)
        if (self.stopped.is_set() or self.operation_cancelled.is_set() or self.websocket is not socket_
                or self._jev_identity != identity or not self.dedicated.available() or self.dedicated.jev_context() != context
                or not isinstance(reply, dict) or reply.get('type') != kind + '_reply' or reply.get('id') != event_id
                or reply.get('run_id') != run_id or reply.get('owner_user_id') != identity[0]
                or reply.get('connection_epoch') != identity[1]):
            raise RuntimeError('bridge_jev_reply_invalid')
        return {key: value for key, value in reply.items()
                if key not in {'type', 'id', 'run_id', 'owner_user_id', 'connection_epoch'}}

    def _jev_deliver(self, message):
        with self._jev_lock:
            waiter = self._jev_waiters.get(message.get('id'))
            if waiter is not None and not waiter['event'].is_set():
                received = tuple(message.get(key) for key in
                                 ('type', 'id', 'run_id', 'owner_user_id', 'connection_epoch'))
                if received == waiter['identity']:
                    waiter['reply'] = message
                waiter['event'].set()

    def _jev_release_waiters(self):
        with self._jev_lock:
            waiters = tuple(self._jev_waiters.values())
        for waiter in waiters:
            waiter['event'].set()

    def stop(self):
        self.stopped.set()
        self.operation_cancelled.set()
        self._jev_release_waiters()
        self._close_media_asset_store()
        socket = self.websocket
        if socket:
            socket.close()
        self._close_browser_backend()
        # Do not stop or reap action supervisors here. They intentionally
        # continue independently after this client/WebSocket exits.

    def _close_browser_backend(self):
        try:
            self.dedicated.close()
        except Exception:
            self.status('専用ブラウザの終了確認が必要です')
        close = getattr(self.edge, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                self.status('ブラウザhelperの終了確認ができません')

    def _close_media_asset_store(self):
        store = getattr(self, 'media_asset_store', None)
        self.media_asset_store = None
        if store is not None:
            store.close()

    def browser_diagnostics(self, *, purpose='general'):
        """Read last-known GUI state only; no helper, filesystem, or CDP I/O."""
        if self.browser_backend not in {'agent_browser', 'managed_profile'}:
            return {'backend': self.browser_backend, 'connected': False, 'selected': False,
                    'blocked': False, 'error': '', 'cached': True, 'last_verified_at': None,
                    'current_connection_verified': False, 'connect_attempts': 0}
        return self.edge.diagnostics(purpose)

    def hello(self):
        # Verify installed bytes before advertising generic browser support.
        # This reads only the fixed helper bundle and never opens a browser.
        try:
            load_helper_bundle(require_pinned=True)
            general_commands = sorted(GENERAL_BROWSER_COMMANDS)
        except Exception:
            general_commands = []
        dedicated_commands = sorted(DEDICATED_BROWSER_COMMANDS) if dedicated_runtime_available() else []
        capabilities = ['computer', 'media_asset', 'media_prepare']
        if self.action_runner is not None and self.action_runner.enabled:
            capabilities.append('apps')
        return {'type': 'hello', 'protocol': 1, 'version': VERSION,
                'hostname': socket.gethostname(),
                'machine_id': self.machine_id, 'boot_id': self.boot_id,
                'apps_actions': sorted(self.action_runner.catalog.actions) if self.action_runner is not None else [],
                'apps_catalog': self.action_runner.catalog.manifest() if self.action_runner is not None else [],
                'capabilities': capabilities + (['browser'] if general_commands else []) +
                                (['dedicated_browser'] if dedicated_commands else []),
                'dedicated_browser_commands': dedicated_commands,
                'dedicated_material_state_guard': bool(dedicated_commands),
                'dedicated_material_state_guard_version': 3 if dedicated_commands else 0,
                'browser_backend': self.browser_backend, 'browser_commands': general_commands,
                'media_preparation_backend': self.media_preparation_backend,
                'media_preparation_commands': sorted(MEDIA_PREPARATION_COMMANDS),
                'edge_connected': bool(general_commands) and self.edge.available(),
                'media_preparation_connected': self.edge.media_available()}

    def _bind_ready(self, reply):
        owner = reply.get('owner_user_id')
        if owner != self.config['token'].split('.')[0]:
            raise RuntimeError('profile_driver_owner_unverified')
        self._jev_identity = (owner, reply.get('connection_epoch'))
        if self.action_runner is not None:
            self.action_runner.bind(owner_user_id=owner, device_id=reply['device_id'],
                                    connection_epoch=reply['connection_epoch'])
        self.edge.bind_connection(owner, reply.get('connection_epoch'), reply.get('browser_profiles'))
        self.dedicated.bind_connection(owner, reply.get('connection_epoch'),
                                      enabled=reply.get('dedicated_browser_enabled', False))
        self.media_asset_store = MediaAssetTransferStore(
            device_id=reply['device_id'], connection_epoch=reply['connection_epoch'],
            browser=self.edge.preparation, cancel_event=self.operation_cancelled)

    def browser_setup(self, action, params, *, purpose='general'):
        """Explicit local GUI operations share the browser/computer lock."""
        if self.browser_backend not in {'agent_browser', 'managed_profile'}:
            raise RuntimeError('agent_browser_setup_unavailable')
        if purpose not in {'general', 'media'}:
            raise ValueError('bridge_browser_purpose_invalid')
        if (action not in ({'discover_targets', 'select_target', 'open_profile'} if purpose == 'media'
                           else {'discover_targets', 'select_target'}) or not isinstance(params, dict)
                or (action == 'discover_targets' and params)
                or (action == 'select_target' and (set(params) != {'tab_id'} or type(params['tab_id']) is not int))
                or (action == 'open_profile' and set(params) != {'platform_account_id'})):
            raise ValueError('agent_browser_setup_invalid')
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            raise RuntimeError('bridge_command_cancelled')
        if not self._dispatch_lock.acquire(blocking=False):
            raise RuntimeError('bridge_busy')
        try:
            return self.edge.local_setup(action, params, purpose=purpose)
        finally:
            self._dispatch_lock.release()

    def _disconnect_browser_backend(self, *, active=False, terminal=False):
        """Revoke old WSS authority without discarding an idle CDP connection."""
        self.operation_cancelled.set()
        self._jev_release_waiters()
        self._close_media_asset_store()
        if active or terminal or self.stopped.is_set():
            # An in-flight action has an uncertain result. Never replay it on
            # a replacement WSS connection or implicitly reattach to Edge.
            self._close_browser_backend()
            return
        suspend = getattr(self.edge, 'suspend', None)
        self.dedicated.suspend()
        if callable(suspend):
            try:
                suspend()
            except Exception:
                self._close_browser_backend()
                self.status('ブラウザ接続の再確認が必要です')
        elif self.browser_backend == 'agent_browser':
            self._close_browser_backend()

    def dispatch(self, message):
        if self.stopped.is_set() or time.time() > message.get('expires_at', 0):
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': 'bridge_command_expired'}
        if not self._dispatch_lock.acquire(blocking=False):
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': 'bridge_busy'}
        try:
            if self.operation_cancelled.is_set():
                raise RuntimeError('bridge_command_cancelled')
            params = message.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('bridge_invalid_params')
            if message.get('channel') == 'browser':
                action = message['action']
                if isinstance(action, str) and action.startswith(DEDICATED_BROWSER_PREFIX):
                    result = self.dedicated._request(action[len(DEDICATED_BROWSER_PREFIX):], params)
                else:
                    result = self.edge._request(action, params)
            elif message.get('channel') == 'media_asset':
                store = getattr(self, 'media_asset_store', None)
                if store is None:
                    raise RuntimeError('bridge_asset_connection_unavailable')
                result = store.dispatch(message['action'], params)
            elif message.get('channel') == 'computer':
                if self.desktop is None:
                    from .desktop import DesktopController
                    self.desktop = DesktopController(stop_event=self.operation_cancelled)
                result = self.desktop.execute(message['action'], params)
            elif message.get('channel') == 'apps':
                runner = self.action_runner
                if runner is None or not runner.enabled:
                    raise RuntimeError('apps_catalog_unavailable')
                result = runner.dispatch(message['action'], params)
            else:
                raise ValueError('bridge_channel_unknown')
            return {'type': 'result', 'id': message['id'], 'ok': True, 'result': result}
        except Exception as exc:
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': str(exc)[:500]}
        finally:
            self._dispatch_lock.release()

    def run(self):
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        url = websocket_url(self.config['server_url'])
        tls = ssl.create_default_context(cafile=self.config.get('ca_file') or None) if url.startswith('wss:') else None
        # One executor thread owns UI Automation/COM objects for the lifetime
        # of the process. It never queues work from a previous connection.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='PC-control') as worker:
            while not self.stopped.is_set():
                future = None
                terminal_connection = False
                self.operation_cancelled.clear()
                try:
                    self.status('接続中…')
                    with connect(url, ssl=tls, additional_headers={'Authorization': 'Bearer ' + self.config['token']},
                                 max_size=16 * 1024 * 1024, open_timeout=10, ping_interval=15, ping_timeout=15) as ws:
                        self.websocket = ws
                        ws.send(json.dumps(self.hello()))
                        reply = json.loads(ws.recv(timeout=10))
                        if reply.get('type') != 'ready':
                            terminal_connection = True
                            raise RuntimeError('bridge_registration_rejected')
                        try:
                            self._bind_ready(reply)
                        except Exception:
                            terminal_connection = True
                            raise
                        self.status('接続済み: ' + str(reply.get('name', 'PC')))
                        future = None
                        last_heartbeat = 0
                        while not self.stopped.is_set():
                            if time.monotonic() - last_heartbeat >= 10:
                                ws.send(json.dumps({'type': 'heartbeat', 'edge_connected': self.edge.available(),
                                                    'media_preparation_connected': self.edge.media_available()}))
                                last_heartbeat = time.monotonic()
                            if future is not None and future.done():
                                ws.send(json.dumps(future.result(), ensure_ascii=False))
                                future = None
                            try:
                                message = json.loads(ws.recv(timeout=0.1))
                            except TimeoutError:
                                continue
                            if message.get('type') == 'command':
                                if future is not None:
                                    ws.send(json.dumps({'type': 'result', 'id': message.get('id'), 'ok': False, 'error': 'bridge_busy'}))
                                else:
                                    future = worker.submit(self.dispatch, message)
                            elif message.get('type') in {'jev_ask_reply', 'jev_gate_reply', 'jev_type_reply'}:
                                self._jev_deliver(message)
                            elif message.get('type') == 'dedicated_browser_gate':
                                # Only the authenticated server connection can change this lease.
                                if message.get('enabled') is not True:
                                    self._jev_release_waiters()
                                self.dedicated.bind_connection(reply['owner_user_id'], reply['connection_epoch'],
                                                              enabled=message.get('enabled', False))
                        if future:
                            future.cancel()
                except ConnectionClosed as exc:
                    self.status('切断: ' + str(exc.code))
                    if exc.code in (4003, 4401, 4403):
                        terminal_connection = True
                        break
                except Exception as exc:
                    # Never log credentials or the Authorization headers.
                    self.status('未接続: ' + type(exc).__name__)
                    if terminal_connection:
                        break
                finally:
                    self.websocket = None
                    self._jev_identity = None
                    self._jev_release_waiters()
                    self._disconnect_browser_backend(active=future is not None,
                                                     terminal=terminal_connection)
                    if future is not None and not future.done():
                        # The already-started operation may finish, but no new
                        # operation can overlap it after reconnect.
                        future.cancel()
                        try:
                            future.result(timeout=35)
                        except Exception:
                            self.stopped.set()
                self.stopped.wait(3)
        self.status('停止しました')
