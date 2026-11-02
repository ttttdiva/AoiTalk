"""Outbound-only WebSocket client with explicit GUI lifecycle and no replay."""
from __future__ import annotations
import asyncio
import copy
import json
import queue
import re
import socket
import ssl
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import VERSION
from .browser_backend import create_browser_backend
from .browser_commands import GENERAL_BROWSER_COMMANDS, MEDIA_PREPARATION_COMMANDS
from .browser_commands import DEDICATED_BROWSER_COMMANDS, DEDICATED_BROWSER_PREFIX
from .dedicated_browser_backend import DedicatedBrowserBridge
from .edge import EdgeBridgeError
from .edge_broker_control import EdgeBrokerControlClient, EdgeBrokerControlError
from .profile_browser_driver import dedicated_runtime_available
from .managed_profile_backend import ManagedProfileBrowserBridge
from .purpose_browser_bridge import PurposeBrowserBridge
from .media_asset_transfer import MediaAssetTransferStore
from .action_identity import boot_id as current_boot_id
from .action_identity import stable_machine_id
from .action_protocol import ActionProtocolError
from .action_runner import ActionRunner


_BROWSER_RECONNECT_OPERATIONS = frozenset({
    "connect", "health_check", "target_list", "target_select", "target_open",
    "read", "verify", "navigate", "action", "other",
})
_BROWSER_RECONNECT_PHASES = frozenset({
    "playwright_disconnect", "playwright_outcome_unknown", "broker_status", "broker_endpoint",
    "playwright_attach", "target_select", "ready",
})
_BROWSER_RECONNECT_TERMINAL_CODES = frozenset({
    "edge_broker_status_unsupported", "edge_broker_invalid_control",
    "edge_broker_invalid_endpoint", "edge_broker_windows_required",
    "browser_setup_target_ambiguous", "browser_setup_target_open_scope_invalid",
    "agent_browser_target_open_scope_invalid",
})
_CROWDWORKS_RECONNECT_ORIGINS = frozenset({
    "https://crowdworks.jp", "https://www.crowdworks.jp",
})
_BROWSER_RECONNECT_BACKOFF_SECONDS = (1, 2, 4, 8, 15)
_BROWSER_HEALTH_PROBE_INTERVAL_SECONDS = 15.0
_BROWSER_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
_BROWSER_HEALTH_PROBE_FAILURE_LIMIT = 3
_BROWSER_RECOVERY_EVENTS = frozenset({
    "disconnect_detected", "reconnect_attempt", "reconnect_ok", "reconnect_failed", "terminal",
})
_BROWSER_RECOVERY_EVENT_PHASES = frozenset({
    *_BROWSER_RECONNECT_PHASES, "dispatch_lock", "playwright_quiescence",
})
_BROWSER_RECOVERY_ERROR_CODES = frozenset({
    *_BROWSER_RECONNECT_TERMINAL_CODES, "edge_broker_client_command_failed",
    "browser_setup_target_not_found", "agent_browser_target_open_failed",
    "browser_setup_target_policy_required", "browser_setup_target_open_rejected",
    "browser_setup_targets_invalid", "browser_setup_selection_unverified",
    "agent_browser_targets_invalid", "edge_broker_client_connect_failed",
    "edge_broker_default_context_unavailable", "edge_broker_storage_unavailable",
    "edge_broker_unavailable", "edge_broker_event_monitor_unavailable",
    "agent_browser_quiescence_unavailable", "agent_browser_disconnect_monitor_unavailable",
    "edge_broker_operation_outcome_unknown", "agent_browser_session_faulted",
    "agent_browser_target_unavailable", "agent_browser_target_gone",
    "agent_browser_rebind_pending", "agent_browser_operation_still_running",
    "browser_setup_recovery_failed", "browser_recovery_dispatch_lock_busy",
    "edge_broker_browser_disconnected", "edge_broker_browser_context_closed",
    "edge_broker_probe_unresponsive",
    "edge_broker_not_ready", "edge_broker_connecting", "edge_broker_failed",
    "edge_broker_reset_required", "edge_broker_timeout", "edge_broker_invalid_response",
    "edge_broker_status_unsupported", "edge_broker_storage_unavailable",
    "edge_broker_invalid_control", "edge_broker_invalid_endpoint", "edge_broker_windows_required",
})


def _reconnect_origin(value):
    """Normalize the origin-only selector kept in Bridge memory for recovery."""
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(char) <= 32 for char in value)):
        raise ValueError("browser_setup_target_prefix_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("browser_setup_target_prefix_invalid") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("browser_setup_target_prefix_invalid")
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


def _select_reconnected_target(backend, policy):
    """Reapply the initial origin selector using only count-based outcomes."""
    open_attempt_count = 0

    def outcome(selected, error, match_count, total_target_count):
        return {
            "selected": selected,
            "error": error,
            "match_count": match_count,
            "total_target_count": total_target_count,
            "target_open_attempt_count": open_attempt_count,
        }

    if not isinstance(policy, dict) or not isinstance(policy.get("origin"), str):
        return outcome(False, "browser_setup_target_policy_required", 0, 0)
    origin = policy["origin"]
    open_if_missing = policy.get("open_target_if_missing") is True
    if open_if_missing and origin not in _CROWDWORKS_RECONNECT_ORIGINS:
        return outcome(False, "browser_setup_target_open_scope_invalid", 0, 0)
    try:
        try:
            discovered = backend.local_setup("discover_targets", {})
        except EdgeBridgeError as exc:
            # The broker-bound context can exist even when its page list is
            # empty. Treat that one explicit outcome like an empty list so the
            # fixed-origin open path below gets a chance to recover it.
            if not (open_if_missing and str(exc) == "agent_browser_no_targets"):
                raise
            discovered = {"targets": []}
        rows = discovered.get("targets") if isinstance(discovered, dict) else None
        if not isinstance(rows, list):
            return outcome(False, "browser_setup_targets_invalid", 0, 0)
        matches = [row for row in rows if isinstance(row, dict) and row.get("origin") == origin
                   and type(row.get("tab_id")) is int]
        if not matches and open_if_missing:
            open_attempt_count = 1
            try:
                opened = backend.local_setup("open_target", {"url": origin + "/"})
            except EdgeBridgeError:
                raise
            except Exception:
                # The IPC outcome may be unknown after the open was accepted.
                # Preserve the one-attempt marker so a retry only rediscovers.
                return outcome(False, "browser_setup_recovery_failed", 0, len(rows))
            rows = opened.get("targets") if isinstance(opened, dict) else None
            if not isinstance(rows, list):
                return outcome(False, "browser_setup_targets_invalid", 0, 0)
            matches = [row for row in rows if isinstance(row, dict) and row.get("origin") == origin
                       and type(row.get("tab_id")) is int]
    except EdgeBridgeError as exc:
        code = str(exc)
        allowed = {
            "edge_broker_endpoint_required", "edge_broker_endpoint_invalid",
            "edge_broker_client_connect_failed", "edge_broker_default_context_unavailable",
            "edge_broker_client_command_failed", "agent_browser_target_open_failed",
            "edge_broker_event_monitor_unavailable",
            "edge_broker_browser_disconnected", "edge_broker_browser_context_closed",
            "agent_browser_target_open_scope_invalid", "agent_browser_targets_invalid",
            "agent_browser_no_targets", "agent_browser_session_faulted",
        }
        return outcome(False, code if code in allowed else "browser_setup_recovery_failed", 0, 0)
    total = len(rows)
    if not matches:
        return outcome(False, "browser_setup_target_not_found", 0, total)
    if len(matches) != 1:
        return outcome(False, "browser_setup_target_ambiguous", len(matches), total)
    try:
        result = backend.local_setup("select_target", {"tab_id": matches[0]["tab_id"]})
    except EdgeBridgeError as exc:
        code = str(exc)
        allowed = {"agent_browser_tab_handle_invalid", "agent_browser_target_binding_failed",
                   "edge_broker_client_command_failed"}
        return outcome(False, code if code in allowed else "browser_setup_recovery_failed", 1, total)
    if not isinstance(result, dict) or result.get("selected") is not True:
        return outcome(False, "browser_setup_selection_unverified", 1, total)
    return outcome(True, None, 1, total)


def _requires_general_browser_connection(message):
    """Separate existing-Edge commands from independent profile runtimes."""
    if not isinstance(message, dict) or message.get('channel') != 'browser':
        return False
    action = message.get('action')
    if isinstance(action, str) and (
            action.startswith(DEDICATED_BROWSER_PREFIX) or action.startswith('media_prepare_')):
        return False
    return True


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
        self._browser_recovery_state_lock = threading.RLock()
        self._browser_transport_lost = threading.Event()
        self._browser_heartbeat_updates = queue.Queue()
        self._browser_recovery_events = queue.Queue(maxsize=1024)
        self._browser_recovery_wakeup = threading.Event()
        self._browser_recovery_thread = None
        self._browser_recovery_attempt_number = 0
        self._browser_recovery_terminal = False
        self._browser_recovery_diagnostic = None
        self._browser_pending_candidate = None
        self._browser_pending_candidate_terminal_code = None
        self._browser_disconnect_generation = 0
        self._browser_reconnect_target_open_attempted = False
        self._browser_probe_failure_count = 0
        self._browser_reconnect_target_policy = None
        self._browser_control = None
        self.websocket = None
        self.browser_backend = 'agent_browser'
        self.media_preparation_backend = 'managed_profile'
        # Local old mode names never select an extension or act as fallback.
        self.edge = PurposeBrowserBridge(
            self._create_browser_backend(config),
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

    def _create_browser_backend(self, config):
        """Construct an agent-browser backend with a fixed-code loss callback."""
        return create_browser_backend(
            {**config, 'browser_backend': 'agent_browser'},
            cancel_event=self.operation_cancelled,
            on_disconnect=self._on_browser_transport_disconnect,
        )

    def set_reconnect_target_policy(self, origin, *, open_target_if_missing=None):
        """Keep only a normalized selector in memory; never persist or report it."""
        normalized = _reconnect_origin(origin)
        if open_target_if_missing is not None and type(open_target_if_missing) is not bool:
            raise ValueError('browser_setup_target_open_selector_invalid')
        with self._browser_recovery_state_lock:
            previous = self._browser_reconnect_target_policy
            same_origin = isinstance(previous, dict) and previous.get('origin') == normalized
            if open_target_if_missing is None:
                open_target_if_missing = (
                    previous.get('open_target_if_missing') is True if same_origin else False
                )
            if open_target_if_missing and normalized not in _CROWDWORKS_RECONNECT_ORIGINS:
                raise ValueError('browser_setup_target_open_scope_invalid')
            policy_changed = not same_origin or previous.get(
                'open_target_if_missing') is not open_target_if_missing
            if policy_changed:
                self._browser_reconnect_target_policy = {
                    'origin': normalized,
                    'open_target_if_missing': open_target_if_missing,
                }
                self._browser_reconnect_target_open_attempted = False
                self._browser_probe_failure_count = 0
            if (self._browser_recovery_diagnostic
                    and self._browser_recovery_diagnostic.get('error_code') in {
                        'browser_setup_target_policy_required', 'browser_setup_target_not_found',
                        'browser_setup_target_ambiguous', 'browser_setup_target_open_scope_invalid',
                        'browser_setup_target_open_rejected', 'browser_setup_targets_invalid',
                    }):
                self._browser_recovery_terminal = False
        self._browser_recovery_wakeup.set()
        if self._browser_transport_lost.is_set():
            self._start_browser_recovery()
        elif self._browser_transport_failed():
            self._on_browser_transport_disconnect('edge_broker_browser_disconnected', 'other')

    def _on_browser_transport_disconnect(self, error_code, operation='other'):
        allowed_codes = {
            'edge_broker_browser_disconnected', 'edge_broker_browser_context_closed',
            'edge_broker_operation_outcome_unknown',
            'edge_broker_client_command_failed', 'agent_browser_session_faulted',
            'agent_browser_target_unavailable', 'agent_browser_target_gone',
            'agent_browser_rebind_pending', 'edge_broker_probe_unresponsive',
        }
        code = error_code if error_code in allowed_codes else 'edge_broker_browser_disconnected'
        operation = operation if operation in _BROWSER_RECONNECT_OPERATIONS else 'other'
        with self._browser_recovery_state_lock:
            if self.stopped.is_set() or self.operation_cancelled.is_set():
                return
            first_loss = not self._browser_transport_lost.is_set()
            self._browser_transport_lost.set()
            self._browser_disconnect_generation += 1
            self._browser_recovery_terminal = False
            if first_loss:
                self._browser_reconnect_target_open_attempted = False
                self._browser_probe_failure_count = 0
        if first_loss:
            # The WSS receive loop sends queued transitions immediately, in order.
            self._browser_heartbeat_updates.put(False)
            self._emit_browser_recovery_transition(
                'disconnect_detected',
                phase=('playwright_outcome_unknown' if code == 'edge_broker_operation_outcome_unknown'
                       else 'playwright_disconnect'),
                error_code=code,
                operation=operation,
            )
        self._browser_recovery_wakeup.set()
        self._set_browser_recovery_diagnostic(
            phase=('playwright_outcome_unknown' if code == 'edge_broker_operation_outcome_unknown'
                   else 'playwright_disconnect'),
            error_code=code, operation=operation)
        try:
            self.status('ブラウザー接続の切断を検知しました')
        except Exception:
            pass
        self._start_browser_recovery()

    def _set_browser_recovery_diagnostic(self, *, phase, error_code=None, operation='other',
                                         match_count=None, total_target_count=None,
                                         target_open_attempt_count=None):
        if phase not in _BROWSER_RECONNECT_PHASES:
            phase = 'playwright_attach'
        if operation not in _BROWSER_RECONNECT_OPERATIONS:
            operation = 'other'
        safe_codes = _BROWSER_RECOVERY_ERROR_CODES | {
            'edge_broker_endpoint_validation_failed',
        }
        if (error_code is not None and error_code not in safe_codes
                and not (isinstance(error_code, str)
                         and re.fullmatch(r'edge_broker_[a-z0-9_]{1,64}', error_code))):
            error_code = 'browser_setup_recovery_failed'
        record = {
            'phase': phase,
            'error_code': error_code,
            'operation': operation,
            'connection_method': 'gateway_browser_bind_websocket',
        }
        if type(match_count) is int and match_count >= 0:
            record['match_count'] = match_count
        if type(total_target_count) is int and total_target_count >= 0:
            record['total_target_count'] = total_target_count
        if type(target_open_attempt_count) is int and target_open_attempt_count in {0, 1}:
            record['target_open_attempt_count'] = target_open_attempt_count
        with self._browser_recovery_state_lock:
            changed = record != self._browser_recovery_diagnostic
            self._browser_recovery_diagnostic = record
        if changed:
            # The callback surface receives only an allowlisted diagnostic record.
            try:
                self.status(json.dumps({'status': 'browser_reconnect', **record}, ensure_ascii=False,
                                       separators=(',', ':')))
            except Exception:
                pass

    def browser_recovery_diagnostics(self):
        """Return only fixed codes, phase, operation, method and counts."""
        with self._browser_recovery_state_lock:
            return copy.deepcopy(self._browser_recovery_diagnostic)

    def _emit_browser_recovery_transition(self, event, *, attempt=None, phase, error_code=None,
                                          operation='other', match_count=None, total_target_count=None,
                                          target_open_attempt_count=None):
        """Queue one fixed-code event; no tab data, URL, endpoint, or exception text."""
        if event not in _BROWSER_RECOVERY_EVENTS:
            return
        if phase not in _BROWSER_RECOVERY_EVENT_PHASES:
            phase = 'playwright_attach'
        if operation not in _BROWSER_RECONNECT_OPERATIONS:
            operation = 'other'
        if error_code is not None and error_code not in _BROWSER_RECOVERY_ERROR_CODES:
            error_code = 'browser_setup_recovery_failed'
        record = {
            'event': event,
            'attempt': attempt if type(attempt) is int and 1 <= attempt <= 1_000_000 else None,
            'phase': phase,
            'error_code': error_code,
            'operation': operation,
            'connection_method': 'gateway_browser_bind_websocket',
        }
        for key, value in (('match_count', match_count), ('total_target_count', total_target_count)):
            if type(value) is int and value >= 0:
                record[key] = value
        if type(target_open_attempt_count) is int and target_open_attempt_count in {0, 1}:
            record['target_open_attempt_count'] = target_open_attempt_count
        try:
            self._browser_recovery_events.put_nowait(record)
        except queue.Full:
            try:
                self._browser_recovery_events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._browser_recovery_events.put_nowait(record)
            except queue.Full:
                pass
        # The ordinary headless client writes this line directly. The setup
        # controller also drains the bounded event queue to flush every line.
        try:
            self.status(json.dumps({'status': 'browser_reconnect_transition', **record},
                                   ensure_ascii=False, separators=(',', ':')))
        except Exception:
            pass

    def drain_browser_recovery_events(self):
        events = []
        while True:
            try:
                events.append(self._browser_recovery_events.get_nowait())
            except queue.Empty:
                return events

    def _browser_is_available(self):
        with self._browser_recovery_state_lock:
            return not self._browser_transport_failed() and self.edge.available()

    @staticmethod
    def _browser_health_probe_due(now, last_probe, *, idle, transport_lost):
        return (
            idle is True and transport_lost is not True
            and type(now) in (int, float) and type(last_probe) in (int, float)
            and now - last_probe >= _BROWSER_HEALTH_PROBE_INTERVAL_SECONDS
        )

    def _probe_browser_transport(self, *, timeout_seconds=_BROWSER_HEALTH_PROBE_TIMEOUT_SECONDS):
        """Actively probe the broker only when the WebSocket command worker is idle."""
        if self._browser_transport_lost.is_set():
            return False
        # GUI setup and command dispatch use the same lock. Never block the
        # WebSocket receive/heartbeat loop behind an unrelated adapter lock.
        if not self._dispatch_lock.acquire(blocking=False):
            return None
        try:
            if self._browser_transport_lost.is_set():
                return False
            general = getattr(self.edge, "general", None)
            probe = getattr(general, "probe_transport", None)
            wait_quiescent = getattr(general, "wait_quiescent", None)
            quiet = True
            if callable(wait_quiescent):
                try:
                    quiet = wait_quiescent(0.0) is True
                except Exception:
                    quiet = False
            if not quiet:
                healthy = None
            elif callable(probe):
                try:
                    healthy = probe(timeout_seconds=timeout_seconds)
                except Exception:
                    healthy = False
            else:
                healthy = False
            if healthy is True:
                self._browser_probe_failure_count = 0
                return self._browser_is_available()
            if healthy is None:
                self._browser_probe_failure_count += 1
                if self._browser_probe_failure_count < _BROWSER_HEALTH_PROBE_FAILURE_LIMIT:
                    # A single bounded version-probe timeout is not proof that
                    # the shared browser is gone. Do not queue another probe
                    # while its command has not become quiescent.
                    return self._browser_is_available()
                self._browser_probe_failure_count = 0
                abort_probe = getattr(general, "abort_pending_health_check", None)
                if callable(abort_probe):
                    try:
                        abort_probe()
                    except Exception:
                        pass
                self._on_browser_transport_disconnect(
                    "edge_broker_probe_unresponsive", "health_check"
                )
                return False
            self._browser_probe_failure_count = 0
            if not self._browser_transport_lost.is_set():
                # An explicit disconnected result or missing probe method is
                # distinct from an individual probe timeout.
                self._on_browser_transport_disconnect("edge_broker_browser_disconnected", "health_check")
            return False
        finally:
            self._dispatch_lock.release()

    def _browser_transport_failed(self):
        if self._browser_transport_lost.is_set():
            return True
        general = getattr(self.edge, 'general', None)
        disconnected = getattr(general, 'transport_disconnected', None)
        if not callable(disconnected):
            return False
        try:
            return disconnected() is True
        except Exception:
            return True

    def _send_browser_health_updates(self, websocket):
        while True:
            try:
                edge_connected = self._browser_heartbeat_updates.get_nowait()
            except queue.Empty:
                return
            websocket.send(json.dumps({
                'type': 'heartbeat',
                'edge_connected': edge_connected is True,
                'media_preparation_connected': self.edge.media_available(),
            }))

    def _start_browser_recovery(self):
        with self._browser_recovery_state_lock:
            if (self.stopped.is_set() or self.operation_cancelled.is_set()
                    or self._browser_recovery_terminal
                    or (self._browser_recovery_thread and self._browser_recovery_thread.is_alive())):
                return
            worker = threading.Thread(target=self._browser_recovery_loop,
                                      name='PC-Bridge-browser-recovery', daemon=True)
            self._browser_recovery_thread = worker
            worker.start()

    def _browser_recovery_loop(self):
        delay_index = 0
        while not self.stopped.is_set() and not self.operation_cancelled.is_set():
            self._browser_recovery_wakeup.clear()
            with self._browser_recovery_state_lock:
                self._browser_recovery_attempt_number += 1
                attempt = self._browser_recovery_attempt_number
            self._emit_browser_recovery_transition(
                'reconnect_attempt', attempt=attempt, phase='dispatch_lock', operation='connect')
            if not self._dispatch_lock.acquire(timeout=0.1):
                self._emit_browser_recovery_transition(
                    'reconnect_failed', attempt=attempt, phase='dispatch_lock',
                    error_code='browser_recovery_dispatch_lock_busy', operation='other')
                delay = _BROWSER_RECONNECT_BACKOFF_SECONDS[
                    min(delay_index, len(_BROWSER_RECONNECT_BACKOFF_SECONDS) - 1)]
                delay_index += 1
                self._browser_recovery_wakeup.wait(delay)
                if self.stopped.is_set() or self.operation_cancelled.is_set():
                    self._finish_browser_recovery()
                    return
                continue
            try:
                outcome = self._attempt_browser_recovery()
            except Exception:
                self._set_browser_recovery_diagnostic(
                    phase='playwright_attach', error_code='browser_setup_recovery_failed', operation='other')
                outcome = 'retry'
            finally:
                self._dispatch_lock.release()

            if outcome == 'ready':
                diagnostic = self.browser_recovery_diagnostics() or {}
                self._emit_browser_recovery_transition(
                    'reconnect_ok', attempt=attempt, phase='ready',
                    operation=diagnostic.get('operation', 'target_select'),
                    match_count=diagnostic.get('match_count'),
                    total_target_count=diagnostic.get('total_target_count'),
                    target_open_attempt_count=diagnostic.get('target_open_attempt_count'))
                if self._finish_browser_recovery():
                    delay_index = 0
                    continue
                return
            if outcome == 'stopped':
                self._finish_browser_recovery()
                return
            if outcome == 'terminal':
                diagnostic = self.browser_recovery_diagnostics() or {}
                self._emit_browser_recovery_transition(
                    'terminal', attempt=attempt, phase=diagnostic.get('phase', 'playwright_attach'),
                    error_code=diagnostic.get('error_code') or 'browser_setup_recovery_failed',
                    operation=diagnostic.get('operation', 'other'),
                    match_count=diagnostic.get('match_count'),
                    total_target_count=diagnostic.get('total_target_count'),
                    target_open_attempt_count=diagnostic.get('target_open_attempt_count'))
                self._finish_browser_recovery(terminal=True)
                return
            diagnostic = self.browser_recovery_diagnostics() or {}
            self._emit_browser_recovery_transition(
                'reconnect_failed', attempt=attempt, phase=diagnostic.get('phase', 'playwright_attach'),
                error_code=diagnostic.get('error_code') or 'browser_setup_recovery_failed',
                operation=diagnostic.get('operation', 'other'),
                match_count=diagnostic.get('match_count'),
                total_target_count=diagnostic.get('total_target_count'),
                target_open_attempt_count=diagnostic.get('target_open_attempt_count'))
            delay = _BROWSER_RECONNECT_BACKOFF_SECONDS[
                min(delay_index, len(_BROWSER_RECONNECT_BACKOFF_SECONDS) - 1)]
            delay_index += 1
            self._browser_recovery_wakeup.wait(delay)
            if self.stopped.is_set() or self.operation_cancelled.is_set():
                self._finish_browser_recovery()
                return

    def _finish_browser_recovery(self, *, terminal=False):
        with self._browser_recovery_state_lock:
            if terminal:
                self._browser_recovery_terminal = True
            if self._browser_recovery_thread is threading.current_thread():
                if not terminal and self._browser_transport_lost.is_set():
                    return True
                self._browser_recovery_thread = None
            return False

    def _close_recovery_candidate(self, candidate, *, terminal_code=None):
        """Close a failed attach candidate and retain it until its executor settles."""
        try:
            candidate.close()
        except Exception:
            pass
        quiesce = getattr(candidate, 'wait_quiescent', None)
        if callable(quiesce):
            try:
                if quiesce(0.0):
                    return True
            except Exception:
                pass
        with self._browser_recovery_state_lock:
            self._browser_pending_candidate = candidate
            self._browser_pending_candidate_terminal_code = terminal_code
        return False

    def _attempt_browser_recovery(self):
        terminal_control_codes = {
            'edge_broker_status_unsupported', 'edge_broker_invalid_control',
            'edge_broker_invalid_endpoint', 'edge_broker_windows_required',
        }
        terminal_target_codes = {
            'browser_setup_target_ambiguous', 'browser_setup_target_open_scope_invalid',
            'agent_browser_target_open_scope_invalid',
        }
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            return 'stopped'
        pending_candidate = getattr(self, '_browser_pending_candidate', None)
        if pending_candidate is not None:
            try:
                pending_candidate.close()
            except Exception:
                pass
            pending_quiesce = getattr(pending_candidate, 'wait_quiescent', None)
            if not callable(pending_quiesce):
                self._set_browser_recovery_diagnostic(
                    phase='playwright_attach', error_code='agent_browser_quiescence_unavailable',
                    operation='other')
                return 'terminal'
            try:
                settled = pending_quiesce(0.0)
            except Exception:
                settled = False
            if not settled:
                self._set_browser_recovery_diagnostic(
                    phase='playwright_attach', error_code='agent_browser_operation_still_running',
                    operation='other')
                return 'retry'
            with self._browser_recovery_state_lock:
                if self._browser_pending_candidate is pending_candidate:
                    self._browser_pending_candidate = None
                    pending_terminal_code = self._browser_pending_candidate_terminal_code
                    self._browser_pending_candidate_terminal_code = None
                else:
                    pending_terminal_code = None
            if pending_terminal_code is not None:
                self._set_browser_recovery_diagnostic(
                    phase='target_select', error_code=pending_terminal_code,
                    operation='target_list')
                return 'terminal'
        with self._browser_recovery_state_lock:
            policy = copy.deepcopy(self._browser_reconnect_target_policy)
            recovery_generation = self._browser_disconnect_generation
            open_attempted = self._browser_reconnect_target_open_attempted
        if policy is None:
            self._set_browser_recovery_diagnostic(
                phase='target_select', error_code='browser_setup_target_policy_required',
                operation='target_list', match_count=0, total_target_count=0)
            return 'retry'

        old_backend = self.edge.general
        try:
            old_backend.close()
        except Exception:
            pass
        quiesce = getattr(old_backend, 'wait_quiescent', None)
        if not callable(quiesce):
            self._set_browser_recovery_diagnostic(
                phase='playwright_attach', error_code='agent_browser_quiescence_unavailable',
                operation='other')
            return 'terminal'
        if not quiesce(0.0):
            self._set_browser_recovery_diagnostic(
                phase='playwright_attach', error_code='agent_browser_operation_still_running',
                operation='other')
            return 'retry'
        with self._browser_recovery_state_lock:
            if recovery_generation != self._browser_disconnect_generation:
                return 'retry'
        if self._browser_control is None:
            self._browser_control = EdgeBrokerControlClient()

        try:
            status = asyncio.run(self._browser_control.get_status(timeout_sec=3.0))
        except EdgeBrokerControlError as exc:
            code = exc.code
            self._set_browser_recovery_diagnostic(
                phase='broker_status', error_code=code, operation='connect')
            return 'terminal' if code in terminal_control_codes else 'retry'
        if status.phase != 'ready' or status.cdp_connected is not True:
            code = status.error_code or 'edge_broker_not_ready'
            if not isinstance(code, str) or not code.startswith('edge_broker_'):
                code = 'edge_broker_not_ready'
            self._set_browser_recovery_diagnostic(
                phase='broker_status', error_code=code, operation='connect')
            return 'retry'
        with self._browser_recovery_state_lock:
            if recovery_generation != self._browser_disconnect_generation:
                return 'retry'
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            return 'stopped'

        try:
            endpoint = asyncio.run(self._browser_control.get_browser_endpoint(timeout_sec=5.0))
        except EdgeBrokerControlError as exc:
            code = exc.code
            self._set_browser_recovery_diagnostic(
                phase='broker_endpoint', error_code=code, operation='connect')
            return 'terminal' if code in terminal_control_codes else 'retry'
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            return 'stopped'
        with self._browser_recovery_state_lock:
            if recovery_generation != self._browser_disconnect_generation:
                return 'retry'

        runtime_config = copy.deepcopy(self.config)
        runtime_config.setdefault('agent_browser', {})['broker_endpoint'] = endpoint.for_connection()
        candidate = None
        try:
            candidate = self._create_browser_backend(runtime_config)
            selection_policy = copy.deepcopy(policy)
            if open_attempted:
                selection_policy['open_target_if_missing'] = False
            selection = _select_reconnected_target(candidate, selection_policy)
            if selection.get('target_open_attempt_count') == 1:
                with self._browser_recovery_state_lock:
                    # A target that appears late may be picked up by a later
                    # discovery, but a single recovery episode opens at most
                    # one tab even if the first listing is stale.
                    self._browser_reconnect_target_open_attempted = True
        except Exception as exc:
            if candidate is not None:
                candidate_code = getattr(exc, 'code', None) or str(exc)
                if candidate_code not in terminal_target_codes:
                    candidate_code = None
                retained = not self._close_recovery_candidate(candidate, terminal_code=candidate_code)
                if retained:
                    self._set_browser_recovery_diagnostic(
                        phase='playwright_attach', error_code='agent_browser_operation_still_running',
                        operation='target_list')
                    return 'retry'
                if candidate_code is not None:
                    self._set_browser_recovery_diagnostic(
                        phase='target_select', error_code=candidate_code, operation='target_list')
                    return 'terminal'
            raise
        if selection['selected'] is not True:
            code = selection['error']
            selection_operation = (
                'target_open' if selection.get('target_open_attempt_count') == 1 else 'target_list'
            )
            retained = not self._close_recovery_candidate(
                candidate, terminal_code=code if code in terminal_target_codes else None)
            if retained:
                self._set_browser_recovery_diagnostic(
                    phase='playwright_attach', error_code='agent_browser_operation_still_running',
                    operation=selection_operation, match_count=selection.get('match_count'),
                    total_target_count=selection.get('total_target_count'),
                    target_open_attempt_count=selection.get('target_open_attempt_count'))
                return 'retry'
            self._set_browser_recovery_diagnostic(
                phase='target_select', error_code=code, operation=selection_operation,
                match_count=selection.get('match_count'),
                total_target_count=selection.get('total_target_count'),
                target_open_attempt_count=selection.get('target_open_attempt_count'))
            return 'terminal' if code in terminal_target_codes else 'retry'
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            self._close_recovery_candidate(candidate)
            return 'stopped'
        disconnect_check = getattr(candidate, 'transport_disconnected', None)
        if not callable(disconnect_check):
            self._close_recovery_candidate(candidate, terminal_code='agent_browser_disconnect_monitor_unavailable')
            self._set_browser_recovery_diagnostic(
                phase='playwright_attach', error_code='agent_browser_disconnect_monitor_unavailable',
                operation='other')
            return 'terminal'
        if disconnect_check():
            self._close_recovery_candidate(candidate)
            return 'retry'
        rejected = None
        with self._browser_recovery_state_lock:
            if self.stopped.is_set() or self.operation_cancelled.is_set():
                rejected = 'stopped'
            elif (recovery_generation != self._browser_disconnect_generation
                    or disconnect_check()):
                rejected = 'retry'
            else:
                self.edge.general = candidate
                self._browser_transport_lost.clear()
                self._browser_recovery_terminal = False
                self._browser_heartbeat_updates.put(True)
                self._set_browser_recovery_diagnostic(
                    phase='ready', error_code=None,
                    operation=('target_open' if selection.get('target_open_attempt_count') == 1
                               else 'target_select'),
                    match_count=selection['match_count'], total_target_count=selection['total_target_count'],
                    target_open_attempt_count=selection.get('target_open_attempt_count'))
        if rejected is not None:
            self._close_recovery_candidate(candidate)
            return rejected
        return 'ready'

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
        self._mark_stopped()
        self._jev_release_waiters()
        self._close_media_asset_store()
        socket = self.websocket
        if socket:
            socket.close()
        self._close_browser_backend()
        # Do not stop or reap action supervisors here. They intentionally
        # continue independently after this client/WebSocket exits.

    def _mark_stopped(self):
        """Serialize shutdown with broker-client publication during recovery."""
        with self._browser_recovery_state_lock:
            self.stopped.set()
            self.operation_cancelled.set()
            self._browser_recovery_wakeup.set()

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
        pending = getattr(self, '_browser_pending_candidate', None)
        if pending is not None:
            try:
                pending.close()
            except Exception:
                pass
            wait_quiescent = getattr(pending, 'wait_quiescent', None)
            if callable(wait_quiescent):
                try:
                    wait_quiescent(0.0)
                except Exception:
                    pass

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
        result = self.edge.diagnostics(purpose)
        if purpose == 'general':
            with self._browser_recovery_state_lock:
                if self._browser_transport_failed():
                    result['connected'] = False
                    result['selected'] = False
                recovery = copy.deepcopy(self._browser_recovery_diagnostic)
            if recovery is not None:
                result.update({
                    'reconnect_phase': recovery['phase'],
                    'reconnect_error_code': recovery['error_code'],
                    'reconnect_operation': recovery['operation'],
                    'connection_method': recovery['connection_method'],
                })
                for key in ('match_count', 'total_target_count'):
                    if key in recovery:
                        result[key] = recovery[key]
        return result

    def hello(self):
        # Existing Edge access is broker-only. Advertising commands is cached
        # metadata and never starts a Playwright connection.
        general_commands = sorted(GENERAL_BROWSER_COMMANDS)
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
                'edge_connected': bool(general_commands) and self._browser_is_available(),
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
        if self._browser_transport_lost.is_set():
            self._start_browser_recovery()
        elif self._browser_transport_failed():
            self._on_browser_transport_disconnect('edge_broker_browser_disconnected', 'other')

    def browser_setup(self, action, params, *, purpose='general'):
        """Explicit local GUI operations share the browser/computer lock."""
        if self.browser_backend not in {'agent_browser', 'managed_profile'}:
            raise RuntimeError('agent_browser_setup_unavailable')
        if purpose not in {'general', 'media'}:
            raise ValueError('bridge_browser_purpose_invalid')
        if purpose == 'general' and self._browser_transport_failed():
            raise EdgeBridgeError('edge_broker_browser_disconnected')
        if (action not in ({'discover_targets', 'select_target', 'open_profile'} if purpose == 'media'
                           else {'discover_targets', 'select_target', 'open_target'}) or not isinstance(params, dict)
                or (action == 'discover_targets' and params)
                or (action == 'select_target' and (set(params) != {'tab_id'} or type(params['tab_id']) is not int))
                or (action == 'open_target' and (set(params) != {'url'} or not isinstance(params.get('url'), str)
                    or params.get('url') not in {
                    'https://crowdworks.jp/', 'https://www.crowdworks.jp/'}))
                or (action == 'open_profile' and set(params) != {'platform_account_id'})):
            raise ValueError('agent_browser_setup_invalid')
        if self.stopped.is_set() or self.operation_cancelled.is_set():
            raise RuntimeError('bridge_command_cancelled')
        if not self._dispatch_lock.acquire(blocking=False):
            raise RuntimeError('bridge_busy')
        try:
            if purpose == 'general' and self._browser_transport_failed():
                raise EdgeBridgeError('edge_broker_browser_disconnected')
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
        if (_requires_general_browser_connection(message) and self._browser_transport_failed()):
            return {'type': 'result', 'id': message.get('id'), 'ok': False,
                    'error': 'edge_broker_browser_disconnected'}
        if not self._dispatch_lock.acquire(blocking=False):
            return {'type': 'result', 'id': message.get('id'), 'ok': False, 'error': 'bridge_busy'}
        try:
            if self.operation_cancelled.is_set():
                raise RuntimeError('bridge_command_cancelled')
            if (_requires_general_browser_connection(message) and self._browser_transport_failed()):
                return {'type': 'result', 'id': message.get('id'), 'ok': False,
                        'error': 'edge_broker_browser_disconnected'}
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
        exit_reason_code = 'bridge_requested_stop'
        exception_details = None
        try:
            exit_reason_code = self._run_connection_loop()
        except BaseException as exc:
            exit_reason_code = 'bridge_unhandled_exception'
            exception_details = self._safe_exception_details(exc)
            raise
        finally:
            record = {'status': 'bridge_exit', 'exit_reason_code': exit_reason_code}
            if exception_details:
                record.update(exception_details)
            try:
                # Headless setup routes status callbacks through a UI queue;
                # stdout is the process log that survives that queue's owner.
                print(json.dumps(record, separators=(',', ':'), ensure_ascii=True), flush=True)
            except Exception:
                # A broken stdout must not replace the original exit.
                try:
                    self.status(json.dumps(record, separators=(',', ':'), ensure_ascii=True))
                except Exception:
                    pass

    @staticmethod
    def _safe_exception_details(exc):
        class_name = type(exc).__name__
        if not isinstance(class_name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,40}', class_name):
            class_name = 'other'
        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        source = None
        if frames:
            frame = frames[-1]
            basename = frame.filename.replace('\\', '/').rsplit('/', 1)[-1]
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', basename) and type(frame.lineno) is int:
                source = f'{basename}:{frame.lineno}'
        details = {'exception_class': class_name}
        if source is not None:
            details['exception_source'] = source
        return details

    def _run_connection_loop(self):
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        url = websocket_url(self.config['server_url'])
        tls = ssl.create_default_context(cafile=self.config.get('ca_file') or None) if url.startswith('wss:') else None
        # One executor thread owns UI Automation/COM objects for the lifetime
        # of the process. It never queues work from a previous connection.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='PC-control') as worker:
            exit_reason_code = 'bridge_requested_stop'
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
                            exit_reason_code = 'bridge_registration_rejected'
                            raise RuntimeError('bridge_registration_rejected')
                        try:
                            self._bind_ready(reply)
                        except Exception:
                            terminal_connection = True
                            exit_reason_code = 'bridge_registration_failed'
                            raise
                        self.status('接続済み: ' + str(reply.get('name', 'PC')))
                        future = None
                        last_heartbeat = 0
                        last_browser_probe = time.monotonic()
                        while not self.stopped.is_set():
                            self._send_browser_health_updates(ws)
                            now = time.monotonic()
                            if self._browser_health_probe_due(
                                    now, last_browser_probe,
                                    idle=future is None,
                                    transport_lost=self._browser_transport_lost.is_set()):
                                self._probe_browser_transport()
                                last_browser_probe = time.monotonic()
                                self._send_browser_health_updates(ws)
                            if time.monotonic() - last_heartbeat >= 10:
                                ws.send(json.dumps({'type': 'heartbeat', 'edge_connected': self._browser_is_available(),
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
                        exit_reason_code = 'bridge_server_rejected_connection'
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
                            exit_reason_code = 'bridge_operation_unsettled'
                            self.stopped.set()
                self.stopped.wait(3)
        self.status('停止しました')
        return exit_reason_code
