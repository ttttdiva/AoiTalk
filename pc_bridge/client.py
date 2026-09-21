"""Outbound-only WebSocket client with explicit GUI lifecycle and no replay."""
from __future__ import annotations
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

from . import VERSION
from .edge import EdgeBrowserBridge


def websocket_url(server):
    url = urlsplit(str(server).strip())
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('サーバーURLは http(s)://host:port の形式で指定してください')
    return urlunsplit(('wss' if url.scheme == 'https' else 'ws', url.netloc,
                      url.path.rstrip('/') + '/api/pc-bridge/connect', '', ''))


class BridgeClient:
    def __init__(self, config, status=lambda value: None):
        self.config = config
        self.status = status
        self.stopped = threading.Event()
        self.operation_cancelled = threading.Event()
        self.websocket = None
        self.edge = EdgeBrowserBridge()
        self.desktop = None

    def stop(self):
        self.stopped.set()
        self.operation_cancelled.set()
        socket = self.websocket
        if socket:
            socket.close()

    def dispatch(self, message):
        if self.stopped.is_set() or time.time() > message.get('expires_at', 0):
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': 'bridge_command_expired'}
        try:
            params = message.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('bridge_invalid_params')
            if message.get('channel') == 'browser':
                result = self.edge._request(message['action'], params)
            elif message.get('channel') == 'computer':
                if self.desktop is None:
                    from .desktop import DesktopController
                    self.desktop = DesktopController(stop_event=self.operation_cancelled)
                result = self.desktop.execute(message['action'], params)
            else:
                raise ValueError('bridge_channel_unknown')
            return {'type': 'result', 'id': message['id'], 'ok': True, 'result': result}
        except Exception as exc:
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': str(exc)[:500]}

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
                self.operation_cancelled.clear()
                try:
                    self.status('接続中…')
                    with connect(url, ssl=tls, additional_headers={'Authorization': 'Bearer ' + self.config['token']},
                                 max_size=16 * 1024 * 1024, open_timeout=10, ping_interval=15, ping_timeout=15) as ws:
                        self.websocket = ws
                        ws.send(json.dumps({'type': 'hello', 'protocol': 1, 'version': VERSION,
                                            'hostname': socket.gethostname(), 'capabilities': ['browser', 'computer'],
                                            'edge_connected': self.edge.available()}))
                        reply = json.loads(ws.recv(timeout=10))
                        if reply.get('type') != 'ready':
                            raise RuntimeError('bridge_registration_rejected')
                        self.status('接続済み: ' + str(reply.get('name', 'PC')))
                        future = None
                        last_heartbeat = 0
                        while not self.stopped.is_set():
                            if time.monotonic() - last_heartbeat >= 10:
                                ws.send(json.dumps({'type': 'heartbeat', 'edge_connected': self.edge.available()}))
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
                        if future:
                            future.cancel()
                except ConnectionClosed as exc:
                    self.status('切断: ' + str(exc.code))
                    if exc.code in (4003, 4401, 4403):
                        break
                except Exception as exc:
                    # Never log credentials or the Authorization headers.
                    self.status('未接続: ' + type(exc).__name__)
                finally:
                    self.websocket = None
                    self.operation_cancelled.set()
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
