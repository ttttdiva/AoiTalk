"""Opt-in Edge-compatible adapter for a manifest-pinned, owned bridge helper.

Only this adapter's opaque handles are action authority. The helper remains the
sole CDP implementation, and the legacy extension is never a fallback. The
adapter projects safe snapshots; page prose is not a general-purpose DLP layer.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import secrets
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .edge import EdgeBridgeError
from .browser_commands import GENERAL_BROWSER_COMMANDS

REQUIRED_CAPABILITIES = frozenset({
    "metadata-targets-v1", "existing-target-v1", "strict-refs-v1",
    "redact-editable-values-v1", "private-command-v1", "local-verify-v1", "persistent-session-v1",
    "existing-target-discovery-v1",
    "local-target-labels-v1",
    "crowdworks-inspection-v1",
})
_ACTION_ROLES = {
    "click": frozenset({"button", "link", "checkbox", "radio", "switch", "tab",
                        "menuitem", "menuitemcheckbox", "menuitemradio", "option",
                        "combobox", "treeitem", "textbox", "searchbox"}),
    "type": frozenset({"textbox", "searchbox"}),
    "select": frozenset({"combobox", "listbox"}),
    "check": frozenset({"checkbox", "switch"}),
    "uncheck": frozenset({"checkbox", "switch"}),
}
_ROLE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_REF = re.compile(r"e[1-9][0-9]{0,19}\Z")
_TARGET = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ATTR = re.compile(r"(?:level=[0-9]+|checked=(?:true|false|mixed)|expanded=(?:true|false)|selected|disabled|required|ref=e[1-9][0-9]{0,19})\Z")
_FACTORY_ERRORS = frozenset({
    "edge_debugging_endpoint_missing", "edge_debugging_endpoint_unreadable", "edge_active_port_invalid",
    "edge_active_port_link_rejected", "edge_profile_path_invalid", "edge_cdp_endpoint_invalid",
    "edge_local_app_data_unavailable", "edge_channel_invalid", "edge_process_verification_requires_windows",
    "edge_listener_owner_unverified", "edge_process_identity_unverified", "edge_process_path_invalid",
    "edge_listener_not_edge", "edge_installation_path_unavailable", "edge_channel_path_mismatch",
    "edge_listener_owner_changed", "edge_process_identity_changed", "edge_listener_not_loopback",
    "edge_listener_table_invalid", "edge_listener_query_failed", "agent_browser_bundle_unavailable",
    "agent_browser_manifest_invalid", "agent_browser_manifest_unavailable", "agent_browser_binary_path_invalid",
    "agent_browser_binary_size_mismatch", "agent_browser_binary_unavailable", "agent_browser_binary_hash_mismatch",
    "agent_browser_cancelled",
})
_ZERO_WIDTH = str.maketrans({"\u00a0": " ", **{c: None for c in "\u200b\u200c\u200d\u2060\ufeff"}})


@dataclass(frozen=True)
class _Reference:
    helper_ref: str
    target_id: str
    epoch: str
    run_id: str
    snapshot_id: str
    role: str
    action: str


class _SnapshotBudget(Exception):
    pass


_SNAPSHOT_INVALID_REASONS = frozenset({
    "envelope_types", "tree_unicode", "structured_ref", "line_grammar",
    "name_json", "name_type", "attribute_grammar", "attribute_value",
    "multiple_refs", "unknown_ref", "repeated_ref", "ref_role_mismatch",
    "ref_name_mismatch", "label_unicode",
})


class _SnapshotInvalid(EdgeBridgeError):
    """Fixed local parse reason; the public error remains unchanged.

    Acceptance diagnostics may record this bounded reason after the synthetic
    snapshot has proved it belongs to the runner's loopback fixture. The
    normal adapter surface still exposes only ``agent_browser_snapshot_invalid``.
    """

    def __init__(self, reason):
        self.reason = reason if reason in _SNAPSHOT_INVALID_REASONS else "unexported_reason"
        super().__init__("agent_browser_snapshot_invalid")


class _HelperFailure(Exception):
    def __init__(self, code: str, *, executed: bool | None = None):
        self.code, self.executed = code, executed
        super().__init__(code)


class AgentBrowserBridge:
    """Synchronous IPC-shaped surface plus an async, cancellation-safe wrapper.

    ``runtime_factory`` is called once by the first explicit ``resolve_tab``.
    The owned helper/CDP connection lasts until Bridge shutdown or a fault; run
    boundaries invalidate action authority without making new browser connections.
    A failed operation never restarts or retries a helper.
    """

    supported_commands = GENERAL_BROWSER_COMMANDS

    def __init__(
        self, runtime=None, *, runtime_factory: Callable[[], Any] | None = None,
        selected_target_id: str | None = None, timeout: float = 35,
        max_candidates: int = 200, max_snapshot_bytes: int = 262144,
        max_output_bytes: int = 1048576, max_refs: int = 4096,
        cancel_event: threading.Event | None = None, default_scope: dict[str, Any] | None = None,
        diagnostic_cache: dict[str, Any] | None = None,
    ):
        if runtime is None and runtime_factory is None:
            raise EdgeBridgeError("agent_browser_runtime_required")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120
                or any(type(value) is not int or value < 1 for value in
                       (max_candidates, max_snapshot_bytes, max_output_bytes, max_refs))
                or max_output_bytes > 16 * 1024 * 1024):
            raise EdgeBridgeError("agent_browser_limits_invalid")
        if selected_target_id is not None and not _valid_target(selected_target_id):
            raise EdgeBridgeError("agent_browser_target_invalid")
        self._runtime, self._factory = runtime, runtime_factory
        self._selected_target = selected_target_id or getattr(runtime, "attach_target", None)
        self.timeout = timeout
        self.max_candidates, self.max_snapshot_bytes = max_candidates, max_snapshot_bytes
        self.max_output_bytes, self.max_refs = max_output_bytes, max_refs
        self._default_scope = {} if default_scope is None else default_scope
        self._default_scope = self._scope({"scope": self._default_scope})[0]
        self._external_cancel = cancel_event
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._closed = self._faulted = self._available = self._running = False
        self._epoch = uuid4().hex
        self._runtime_session = _runtime_identity(runtime)
        self._connected = self._preflight_done = self._connect_attempted = False
        self._run_id: str | None = None
        self._snapshot_id: str | None = None
        self._handles: dict[int, str] = {}
        self._refs: dict[str, _Reference] = {}
        self._verification_refs: dict[str, _Reference] = {}
        self._bound_target: str | None = None
        self._current_handle: int | None = None
        self._diagnostic_error = ""
        self._last_verified_at = None
        self._diagnostic_cache = diagnostic_cache if diagnostic_cache is not None else {}

    def diagnostics(self):
        """Cached GUI state: no lock wait, process, filesystem, or CDP request."""
        blocked = self._faulted or self._closed
        connected = self._connected and not blocked
        return {"connected": connected, "selected": connected and self._bound_target is not None,
                "blocked": blocked, "error": self._diagnostic_error,
                "connect_attempts": int(self._connect_attempted), "permission_pending": None,
                "selection_required": connected and self._bound_target is None,
                "cached": True, "last_verified_at": self._last_verified_at,
                "current_connection_verified": False,
                "edge_policy": deepcopy(self._diagnostic_cache.get("edge_policy"))}

    def available(self) -> bool:
        """Cached health only: hello/heartbeat cannot spawn or connect anything."""
        return bool(self._available and not self._closed and not self._faulted and not self._cancelled())

    def _cancelled(self):
        return self._cancel.is_set() or bool(self._external_cancel and self._external_cancel.is_set())

    def _invalidate(self, *, epoch=False, connection=False):
        self._refs.clear()
        self._verification_refs.clear()
        self._snapshot_id = None
        if epoch:
            self._epoch = uuid4().hex
            self._run_id = None
            self._running = False
        if connection:
            self._handles.clear()
            self._current_handle = self._bound_target = None
            self._connected = False

    def close(self, cancel=True) -> bool:
        # Do not wait for _request's lock: stop must interrupt a blocked child.
        if cancel:
            self._cancel.set()
        self._closed = True
        self._available = False
        self._invalidate(epoch=True, connection=True)
        runtime = self._runtime
        try:
            return runtime is None or runtime.stop() is True
        except Exception:
            return False

    async def request(self, command: str, **params: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._request, command, params)
        except asyncio.CancelledError:
            self._cancel.set()
            self._closed = True
            self._available = False
            self._invalidate(epoch=True, connection=True)
            await asyncio.to_thread(self.close)
            raise

    def local_setup(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        """Local GUI-only projection; never reachable through the WSS entry."""
        if command not in {"discover_targets", "select_target"}:
            raise EdgeBridgeError("agent_browser_setup_invalid")
        return self._request(command, params, _local_setup=True)

    def _request(self, command: str, params: dict[str, Any], *, _local_setup=False) -> dict[str, Any]:
        if not isinstance(command, str) or not isinstance(params, dict):
            raise EdgeBridgeError("agent_browser_params_invalid")
        with self._lock:
            if command == "end_run":
                if self._run_id is not None and params.get("run_id", self._run_id) != self._run_id:
                    raise EdgeBridgeError("agent_browser_run_mismatch")
                return self._end_run()
            if command not in GENERAL_BROWSER_COMMANDS | {"discover_targets", "select_target"}:
                raise EdgeBridgeError("agent_browser_command_unsupported")
            if command in {"discover_targets", "select_target"} and not _local_setup:
                raise EdgeBridgeError("agent_browser_local_setup_required")
            if self._closed or self._cancelled():
                raise EdgeBridgeError("agent_browser_cancelled")
            if self._faulted:
                raise EdgeBridgeError("agent_browser_session_faulted")
            if self._runtime is not None and self._runtime_session != _runtime_identity(self._runtime):
                self._fault()
                raise EdgeBridgeError("agent_browser_epoch_changed")
            if command in {"discover_targets", "select_target"}:
                if self._running:
                    raise EdgeBridgeError("agent_browser_run_active")
                return self._discover_targets() if command == "discover_targets" else self._select_target(params)
            if command == "resolve_tab":
                return self._resolve_tab(params)
            if not self._running:
                raise EdgeBridgeError("agent_browser_run_required")
            if (params.get("run_id", self._run_id) != self._run_id
                    or params.get("epoch", self._epoch) != self._epoch):
                if command == "act":
                    return self._blocked("edge_stale_element")
                raise EdgeBridgeError("agent_browser_run_mismatch")
            try:
                handle, target = self._target(params.get("tab_id"))
            except EdgeBridgeError:
                if command == "act":
                    return self._blocked("edge_stale_element")
                raise
            if command == "observe":
                return self._observe(params, handle, target)
            if command == "act":
                return self._act(params, handle, target)
            if command == "verify":
                return self._verify(params, target)
            if command == "crowdworks_inspect":
                return self._crowdworks_inspect(target)
            return self._page_action(command, params, handle, target)

    def _fault(self):
        self._faulted = True
        self._available = False
        self._diagnostic_error = "agent_browser_reconnect_required"
        self._invalidate(epoch=True, connection=True)
        try:
            if self._runtime is not None:
                self._runtime.stop()
        except Exception:
            pass

    def _runtime_call(self, args, *, private=False):
        if self._cancelled() or self._closed:
            raise _HelperFailure("agent_browser_cancelled", executed=False)
        epoch = self._epoch
        stdin = None
        if private:
            try:
                stdin = json.dumps({"argv": list(args)}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (UnicodeError, ValueError):
                raise _HelperFailure("agent_browser_input_invalid", executed=False) from None
            if len(stdin) > 65536:
                raise _HelperFailure("agent_browser_input_too_large", executed=False)
            args = ("bridge-command", "--stdin")
        try:
            response = self._runtime.run(tuple(args), stdin_bytes=stdin, timeout_seconds=self.timeout,
                                         cancel_event=self, max_output_bytes=self.max_output_bytes)
        except Exception as exc:
            # Runtime error strings/tracebacks may contain private input. Never
            # copy them onto the WSS surface, including in exception chaining.
            executed = None if getattr(exc, "outcome_unknown", True) else False
            code = "agent_browser_outcome_unknown" if executed is None else "agent_browser_command_failed"
            if getattr(exc, "code", None) == "agent_browser_output_limit":
                code = "agent_browser_output_limit"
            self._fault()
            raise _HelperFailure(code, executed=executed) from None
        if epoch != self._epoch or self._cancelled() or self._closed:
            self._fault()
            raise _HelperFailure("agent_browser_outcome_unknown")
        if not isinstance(response, dict) or type(response.get("success")) is not bool:
            self._fault()
            raise _HelperFailure("agent_browser_response_invalid")
        if response["success"] is False:
            data = response.get("data")
            executed = False if isinstance(data, dict) and data.get("executed") is False else None
            code = response.get("code")
            # Only a finite ABI code set, never helper free text, escapes here.
            safe_codes = {"STALE_REF", "STRICT_REF_REQUIRED", "STRICT_ACTION_UNSUPPORTED",
                          "DIALOG_BLOCKED", "FOCUS_CHANGED", "rebind_pending", "tab_gone",
                          "SAFE_SNAPSHOT_UNAVAILABLE", "SAFE_SNAPSHOT_REQUIRED",
                          "REDACTION_UNAVAILABLE", "STRICT_REFS_UNAVAILABLE", "outcome_unknown",
                          "EXACT_TARGET_REQUIRED", "target_unavailable", "STRICT_INPUT_UNSUPPORTED",
                          "connection_blocked", "connection_required", "TARGET_SELECTION_REQUIRED",
                          "CROWDWORKS_SCOPE_UNSUPPORTED", "CROWDWORKS_SCOPE_OVERFLOW",
                          "CROWDWORKS_INSPECTION_UNAVAILABLE"}
            code = code if code in safe_codes else "agent_browser_command_failed"
            if executed is not False or code in {"rebind_pending", "tab_gone", "target_unavailable",
                                                "EXACT_TARGET_REQUIRED", "connection_blocked", "connection_required"}:
                self._fault()
            raise _HelperFailure(code, executed=executed)
        data = response.get("data")
        if not isinstance(data, dict):
            self._fault()
            raise _HelperFailure("agent_browser_response_invalid")
        self._last_verified_at = time.time()
        return data

    def is_set(self):
        """Combined cancellation event interface consumed by OwnedHelperRuntime."""
        return self._cancelled() or self._closed

    def _read(self, args, *, private=False):
        try:
            return self._runtime_call(args, private=private)
        except _HelperFailure as exc:
            self._diagnostic_error = _public_error(exc.code)
            raise EdgeBridgeError(_public_error(exc.code)) from None

    def _begin(self):
        if self._preflight_done:
            return
        if self._runtime is None:
            if self._factory is None:
                raise EdgeBridgeError("agent_browser_fresh_runtime_required")
            try:
                self._runtime = self._factory()
            except Exception as exc:
                code = str(exc)
                self._faulted = True
                self._available = False
                self._diagnostic_error = code if code in _FACTORY_ERRORS else "agent_browser_runtime_unavailable"
                raise EdgeBridgeError(self._diagnostic_error) from None
            self._runtime_session = _runtime_identity(self._runtime)
        try:
            version = self._runtime.preflight(timeout_seconds=min(self.timeout, 10), cancel_event=self)
        except Exception:
            self._fault()
            raise EdgeBridgeError("agent_browser_preflight_failed") from None
        if (not isinstance(version, dict) or type(version.get("bridgeProtocol")) is not int
                or version["bridgeProtocol"] != 1 or not isinstance(version.get("capabilities"), list)
                or any(not isinstance(cap, str) for cap in version["capabilities"])
                or not REQUIRED_CAPABILITIES.issubset(version["capabilities"])):
            self._fault()
            raise EdgeBridgeError("agent_browser_bridge_contract_mismatch")
        self._preflight_done = True

    def _connect_initial(self, target=None):
        self._begin()
        if self._connected:
            return
        if self._connect_attempted:
            self._fault()
            raise EdgeBridgeError("agent_browser_reconnect_required")
        initial = getattr(self._runtime, "attach_target", None)
        discovery = getattr(self._runtime, "discover_existing", False) is True
        if not discovery:
            target = target or self._selected_target or initial
            if not target:
                raise EdgeBridgeError("agent_browser_target_selection_required")
            if initial is not None and initial != target:
                raise EdgeBridgeError("agent_browser_target_mismatch")
            if initial is None:
                try:
                    self._runtime.set_attach_target(target)
                except Exception:
                    self._fault()
                    raise EdgeBridgeError("agent_browser_target_binding_failed") from None
            initial = target
        self._connect_attempted = True
        try:
            connected = self._read(("connect", self._runtime.endpoint.url))
            if connected.get("completed") is not True:
                raise EdgeBridgeError("agent_browser_connection_unverified")
        except EdgeBridgeError as exc:
            self._fault()
            # _read exposes only fixed public codes. Latch the connection fault
            # without replacing its cause with the generic recovery instruction.
            self._diagnostic_error = str(exc)
            raise
        self._connected = True
        self._bound_target = None if discovery else initial
        self._diagnostic_error = ""

    def _handle_for_target(self, target):
        existing = next((key for key, value in self._handles.items() if value == target), None)
        if existing is not None:
            return existing
        handle = secrets.randbelow((1 << 53) - 1) + 1
        while handle in self._handles:
            handle = secrets.randbelow((1 << 53) - 1) + 1
        self._handles[handle] = target
        return handle

    def _discover_targets(self):
        self._connect_initial()
        data = self._read(("tab", "list", "--local-labels"), private=True)
        tabs = data.get("tabs")
        if not isinstance(tabs, list) or len(tabs) > 4096:
            self._fault()
            raise EdgeBridgeError("agent_browser_targets_invalid")
        seen, targets = set(), []
        for item in tabs:
            if (not isinstance(item, dict) or not _valid_target(item.get("targetId"))
                    or item.get("type") != "page" or type(item.get("active")) is not bool
                    or item["targetId"] in seen):
                self._fault()
                raise EdgeBridgeError("agent_browser_targets_invalid")
            target = item["targetId"]
            seen.add(target)
            if item["active"] != (target == self._bound_target):
                self._fault()
                raise EdgeBridgeError("agent_browser_target_changed")
            title = item.get("title")
            if not isinstance(title, str) or len(title) > 512 or any(
                    ord(char) < 32 or ord(char) == 127 or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
                    for char in title):
                self._fault()
                raise EdgeBridgeError("agent_browser_targets_invalid")
            targets.append({"tab_id": self._handle_for_target(target), "origin": _origin(item.get("origin", "")),
                            "title": title, "type": "page", "selected": item["active"]})
        if self._bound_target is not None and self._bound_target not in seen:
            self._fault()
            raise EdgeBridgeError("agent_browser_target_gone")
        self._handles = {handle: target for handle, target in self._handles.items() if target in seen}
        self._diagnostic_error = "" if targets else "agent_browser_no_targets"
        return {"targets": targets, "selection_required": self._bound_target is None,
                "available": bool(targets), "error": self._diagnostic_error}

    def _select_target(self, params):
        handle = params.get("tab_id")
        if not self._connected or type(handle) is not int or handle not in self._handles:
            raise EdgeBridgeError("agent_browser_tab_handle_invalid")
        target = self._handles[handle]
        self._invalidate(epoch=True)
        if target != self._bound_target:
            self._read(("tab", target))
            self._bound_target = target
        self._assert_bound(target)
        self._remember_initial_selection(target)
        self._selected_target, self._current_handle = target, handle
        self._available = True
        self._diagnostic_error = ""
        return {"selected": True, "tab_id": handle}

    def _remember_initial_selection(self, target):
        if (getattr(self._runtime, "discover_existing", False) is True
                and getattr(self._runtime, "attach_target", None) is None):
            try:
                # Bootstrap mode keeps its fixed config/fingerprint. Runtime
                # records this initial authority only in memory after attach.
                self._runtime.set_attach_target(target)
            except Exception:
                self._fault()
                raise EdgeBridgeError("agent_browser_target_binding_failed") from None

    def _resolve_tab(self, params):
        requested = params.get("target_id")
        if requested is not None and not _valid_target(requested):
            raise EdgeBridgeError("agent_browser_target_invalid")
        handle = params.get("tab_id")
        if handle is not None:
            if type(handle) is not int or handle not in self._handles:
                raise EdgeBridgeError("agent_browser_tab_handle_invalid")
            target = self._handles[handle]
            if requested is not None and requested != target:
                raise EdgeBridgeError("agent_browser_target_mismatch")
        else:
            target = requested or self._selected_target
        if not target:
            raise EdgeBridgeError("agent_browser_target_selection_required")
        run_id = params.get("run_id", self._run_id or uuid4().hex)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise EdgeBridgeError("agent_browser_run_invalid")
        if self._running and run_id != self._run_id:
            raise EdgeBridgeError("agent_browser_run_mismatch")
        url = params.get("url", "")
        if url:
            _navigation_url(url)
        self._invalidate()
        self._connect_initial(target)
        if target != self._bound_target:
            # An explicit target change uses the already-approved connection.
            # The helper's pending marker is preserved; there is no reconnect.
            try:
                self._read(("tab", target))
            except EdgeBridgeError:
                self._fault()
                raise
            self._bound_target = target
        active = self._assert_bound(target)
        self._remember_initial_selection(target)
        self._run_id, self._running = run_id, True
        if handle is None:
            handle = self._handle_for_target(target)
        self._current_handle = handle
        self._available = True
        result = {"tab_id": handle, "url": _origin(active.get("origin", "")),
                  "run_id": run_id, "epoch": self._epoch}
        if url:
            outcome = self._page_action("navigate", {"url": url}, handle, target)
            if not outcome["ok"]:
                raise EdgeBridgeError(outcome["error"])
            result["url"] = outcome.get("url", "")
        return result

    def _target(self, handle):
        if handle is None:
            handle = self._current_handle
        if type(handle) is not int or handle not in self._handles:
            raise EdgeBridgeError("agent_browser_tab_handle_invalid")
        target = self._handles[handle]
        if target != self._bound_target:
            raise EdgeBridgeError("agent_browser_target_mismatch")
        return handle, target

    def _assert_bound(self, target):
        data = self._read(("tab", "list"))
        tabs = data.get("tabs")
        if not isinstance(tabs, list) or len(tabs) > 4096:
            self._fault()
            raise EdgeBridgeError("agent_browser_targets_invalid")
        active = [item for item in tabs if isinstance(item, dict) and item.get("active") is True]
        if len(active) != 1 or active[0].get("targetId") != target or active[0].get("type") != "page":
            self._fault()
            raise EdgeBridgeError("agent_browser_target_changed")
        return active[0]

    def _scope(self, params):
        raw = params.get("scope", self._default_scope)
        if not isinstance(raw, dict) or set(raw) - {"selector", "depth"}:
            raise EdgeBridgeError("agent_browser_scope_invalid")
        scope = dict(raw)
        for key in ("selector", "depth"):
            if key in params:
                if key in scope and scope[key] != params[key]:
                    raise EdgeBridgeError("agent_browser_scope_invalid")
                scope[key] = params[key]
        args = ["snapshot", "--interactive", "--redact-editable-values"]
        if "selector" in scope:
            selector = scope["selector"]
            if not isinstance(selector, str) or not selector or len(selector) > 4096 or selector.startswith("--") or "\0" in selector:
                raise EdgeBridgeError("agent_browser_scope_invalid")
            args.extend(("--selector", selector))
        if "depth" in scope:
            depth = scope["depth"]
            if type(depth) is not int or not 1 <= depth <= 100:
                raise EdgeBridgeError("agent_browser_scope_invalid")
            args.extend(("--depth", str(depth)))
        return scope, args

    def _crowdworks_inspect(self, target):
        # The helper owns this fixed, read-only projection. cw_N identifiers
        # are observations, never strict refs or authority for later actions.
        self._invalidate()
        before = self._assert_bound(target)
        if not isinstance(before.get("origin"), str) or before["origin"] not in _CROWDWORKS_ORIGINS:
            raise EdgeBridgeError("crowdworks_selected_target_required")
        data = self._read(("crowdworks-inspect",), private=True)
        after = self._assert_bound(target)
        try:
            result = _crowdworks_projection(data)
            if result["origin"] != before.get("origin") or result["origin"] != after.get("origin"):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            self._fault()
            raise EdgeBridgeError("crowdworks_inspection_invalid") from None
        return result

    def _observe(self, params, handle, target):
        action = params.get("action", "read")
        if not isinstance(action, str) or action not in {*_ACTION_ROLES, "read"}:
            raise EdgeBridgeError("agent_browser_action_unsupported")
        scope, args = self._scope(params)
        self._invalidate()
        self._assert_bound(target)
        epoch = self._epoch
        try:
            data = self._read(args)
        except EdgeBridgeError as exc:
            if str(exc) != "agent_browser_output_limit":
                raise
            return self._scope_required({"tab_id": handle, "url": "", "text": "", "candidates": {},
                                         "coverage": {"complete": False, "scope": scope}},
                                        "snapshot_output_budget_exceeded")
        # A second metadata assertion rejects an unexpected helper target switch
        # without inventing target metadata in the snapshot itself.
        self._assert_bound(target)
        tree, refs = data.get("snapshot"), data.get("refs")
        result = {"tab_id": handle, "url": _origin(data.get("origin", "")), "text": "",
                  "candidates": {}, "run_id": self._run_id, "epoch": self._epoch,
                  "coverage": {"complete": False, "scope": scope}}
        if not isinstance(tree, str) or not isinstance(refs, dict):
            raise _SnapshotInvalid("envelope_types")
        try:
            oversized = len(tree.encode("utf-8")) > self.max_snapshot_bytes
        except UnicodeError:
            raise _SnapshotInvalid("tree_unicode") from None
        if oversized or len(refs) > self.max_refs:
            return self._scope_required(result, "snapshot_budget_exceeded")
        try:
            nodes = _parse_snapshot(tree, refs, max_bytes=self.max_snapshot_bytes)
        except _SnapshotBudget:
            return self._scope_required(result, "projection_budget_exceeded")
        eligible = [node for node in nodes if node["ref"] is not None and not node["disabled"]
                    and (action == "read" or node["role"] in _ACTION_ROLES[action])]
        if len(eligible) > self.max_candidates:
            return self._scope_required(result, "candidate_budget_exceeded", len(eligible))
        snapshot_id = uuid4().hex
        self._snapshot_id = snapshot_id
        result["snapshot_id"] = snapshot_id
        # The safe projection contains no raw helper ref, editable value, URL,
        # DOM tag, frame ID, title, or presumed enabled state.
        result["text"] = "\n".join("  " * node["depth"] + node["role"] +
                                    (" " + node["label"] if node["label"] else "") for node in nodes)
        for node in eligible:
            opaque = "abr_" + uuid4().hex
            self._refs[opaque] = _Reference(node["ref"], target, self._epoch, self._run_id,
                                           snapshot_id, node["role"], action)
            candidate = {"role": node["role"], "label": node["label"]}
            if node["context"]:
                candidate["context"] = node["context"]
            result["candidates"][opaque] = candidate
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.max_snapshot_bytes:
            self._invalidate()
            return self._scope_required(result, "projection_budget_exceeded")
        if self._closed or self._cancelled() or self._epoch != epoch:
            self._invalidate()
            raise EdgeBridgeError("agent_browser_cancelled")
        result["coverage"].update({"complete": True, "candidate_count": len(eligible),
                                    "rendered_ref_count": sum(node["ref"] is not None for node in nodes),
                                    "structured_ref_count": len(refs), "domain": "helper_safe_snapshot"})
        return result

    @staticmethod
    def _scope_required(result, reason, count=None):
        result["text"] = ""
        result["candidates"] = {}
        result.pop("snapshot_id", None)
        result["error"] = "candidate_scope_required"
        result["coverage"].update({"complete": False, "reason": reason})
        if count is not None:
            result["coverage"]["candidate_count"] = count
        return result

    def _reference(self, params, target, *, verification=False):
        opaque = params.get("target")
        if not isinstance(opaque, str):
            return None
        ref = self._refs.get(opaque)
        if ref is None and verification:
            ref = self._verification_refs.get(opaque)
        if (ref is None or ref.epoch != self._epoch or ref.run_id != self._run_id
                or ref.target_id != target or ref.snapshot_id != self._snapshot_id
                or params.get("snapshot_id", ref.snapshot_id) != ref.snapshot_id):
            return None
        return ref

    def _act(self, params, handle, target):
        action = params.get("action")
        ref = self._reference(params, target)
        if ref is None:
            return self._blocked("edge_stale_element", handle)
        if not isinstance(action, str) or action not in _ACTION_ROLES or action != ref.action or ref.role not in _ACTION_ROLES[action]:
            return self._blocked("edge_target_not_actionable", handle)
        value = params.get("value", "")
        if not isinstance(value, str) or "\0" in value:
            return self._blocked("agent_browser_input_invalid", handle)
        if action == "select" and not value:
            return self._blocked("agent_browser_input_invalid", handle)
        try:
            self._assert_bound(target)
        except EdgeBridgeError:
            return self._blocked("edge_stale_element", handle)
        args = ["fill" if action == "type" else action, "@" + ref.helper_ref]
        if action in {"type", "select"}:
            args.append(value)
        self._verification_refs = self._refs
        self._refs = {}  # One snapshot authorizes at most one dispatched action.
        return self._dispatch_action(args, handle, private=True)

    @staticmethod
    def _blocked(error, handle=None):
        result = {"ok": False, "error": error, "executed": False, "execution_status": "blocked",
                  "verification": {"status": "not_performed"}}
        if handle is not None:
            result["tab_id"] = handle
        return result

    def _dispatch_action(self, args, handle, *, private):
        try:
            data = self._runtime_call(args, private=private)
        except _HelperFailure as exc:
            if exc.executed is False:
                return self._blocked(_public_error(exc.code), handle)
            return {"ok": False, "tab_id": handle, "error": "agent_browser_outcome_unknown",
                    "executed": None, "execution_status": "unknown", "verification": {"status": "unknown"}}
        if data.get("completed") is not True:
            self._fault()
            return {"ok": False, "tab_id": handle, "error": "agent_browser_outcome_unknown",
                    "executed": None, "execution_status": "unknown", "verification": {"status": "unknown"}}
        result = {"ok": True, "tab_id": handle, "executed": True, "execution_status": "executed",
                  "verification": {"status": "unknown"}}
        if "origin" in data:
            result["url"] = _origin(data["origin"])
        return result

    def _page_action(self, command, params, handle, target):
        if command == "navigate":
            url = _navigation_url(params.get("url"))
            args = ["open", url]
        elif command == "scroll":
            direction = params.get("direction", "down")
            if not isinstance(direction, str) or direction not in {"up", "down"}:
                return self._blocked("agent_browser_direction_invalid", handle)
            args = ["scroll", direction]
        else:
            args = ["back"]
        self._assert_bound(target)
        self._invalidate()
        return self._dispatch_action(args, handle, private=True)

    def _verify(self, params, target):
        args = ["verify"]
        for key, flag in (("expected_url", "--expected-url"), ("expected_text", "--expected-text")):
            value = params.get(key, "")
            if not isinstance(value, str) or "\0" in value:
                raise EdgeBridgeError("agent_browser_verify_invalid")
            if value:
                if key == "expected_url":
                    _navigation_url(value)
                args.extend((flag, value))
        if "expected_value" in params:
            value = params["expected_value"]
            ref = self._reference(params, target, verification=True)
            if ref is None:
                raise EdgeBridgeError("edge_stale_element")
            if not isinstance(value, str) or "\0" in value:
                raise EdgeBridgeError("agent_browser_verify_invalid")
            args.extend(("--ref", "@" + ref.helper_ref, "--expected-value", value))
        if len(args) == 1:
            raise EdgeBridgeError("agent_browser_verify_condition_required")
        self._assert_bound(target)
        data = self._read(args, private=True)
        if set(data) != {"matched"} or type(data["matched"]) is not bool:
            self._fault()
            raise EdgeBridgeError("agent_browser_verify_response_invalid")
        return {"matched": data["matched"]}

    def _end_run(self):
        # A run is an action-authority boundary, not a browser-connection
        # boundary. Preserve approved CDP and connection-bound target handles.
        # In particular, end_run cannot clear a latched transport/pending fault.
        self._invalidate(epoch=True)
        return {"ended": True, "refs_invalidated": True,
                "connection_retained": self._connected and not self._faulted and not self._closed}

    def suspend(self):
        """Invalidate an idle WSS run while retaining approved browser access.

        The caller must use close() for a command whose outcome is uncertain or
        final Bridge shutdown. This does not clear cancellation or fault latches.
        """
        with self._lock:
            return self._end_run()


# Both names describe the same general helper surface.
AgentBrowserAdapter = AgentBrowserBridge


def _runtime_identity(runtime):
    return (getattr(runtime, "session_id", None), getattr(getattr(runtime, "endpoint", None), "url", None))


def _valid_target(value):
    return isinstance(value, str) and bool(_TARGET.fullmatch(value))


def _public_error(code):
    return {"STALE_REF": "edge_stale_element", "STRICT_REF_REQUIRED": "edge_stale_element",
            "rebind_pending": "agent_browser_rebind_pending", "tab_gone": "agent_browser_target_gone",
            "DIALOG_BLOCKED": "agent_browser_dialog_blocked",
            "FOCUS_CHANGED": "agent_browser_focus_required",
            "TARGET_SELECTION_REQUIRED": "agent_browser_target_selection_required",
            "CROWDWORKS_SCOPE_UNSUPPORTED": "crowdworks_scope_unsupported",
            "CROWDWORKS_SCOPE_OVERFLOW": "crowdworks_scope_overflow",
            "CROWDWORKS_INSPECTION_UNAVAILABLE": "crowdworks_inspection_unavailable",
            "connection_blocked": "agent_browser_connection_blocked",
            "connection_required": "agent_browser_connection_required"}.get(code, code if code.startswith("agent_browser_")
                                                                   else "agent_browser_command_blocked")


_CROWDWORKS_ORIGINS = frozenset({"https://crowdworks.jp", "https://www.crowdworks.jp"})
_CROWDWORKS_HREF = re.compile(r"https://crowdworks\.jp/public/jobs/[0-9]+\Z")


def _crowdworks_projection(data):
    """Validate the complete v1 safe projection, without truncating any field."""
    if (set(data) != {"origin", "candidates", "text", "coverage"}
            or data.get("origin") not in _CROWDWORKS_ORIGINS
            or not isinstance(data.get("candidates"), dict)
            or not isinstance(data.get("text"), str)
            or not isinstance(data.get("coverage"), dict)):
        raise ValueError
    candidates, coverage = data["candidates"], data["coverage"]
    count = len(candidates)
    if (count > 200 or set(candidates) != {f"cw_{index}" for index in range(count)}
            or set(coverage) != {"complete", "frame_scope", "shadow_scope", "candidate_count", "truncated", "unscanned"}
            or coverage.get("complete") is not True or coverage.get("truncated") is not False
            or coverage.get("unscanned") is not False or coverage.get("frame_scope") != "main"
            or coverage.get("shadow_scope") != "none"
            or type(coverage.get("candidate_count")) is not int or coverage["candidate_count"] != count):
        raise ValueError
    total = len(data["text"].encode("utf-8"))
    if total > 131072:
        raise ValueError
    for candidate in candidates.values():
        if (not isinstance(candidate, dict) or set(candidate) != {"href", "label", "context"}
                or any(not isinstance(candidate.get(key), str) for key in ("href", "label", "context"))
                or not _CROWDWORKS_HREF.fullmatch(candidate["href"])):
            raise ValueError
        label_bytes, context_bytes = len(candidate["label"].encode("utf-8")), len(candidate["context"].encode("utf-8"))
        if label_bytes > 4096 or context_bytes > 16000:
            raise ValueError
        total += len(candidate["href"].encode("utf-8")) + label_bytes + context_bytes
    if (total > 524288 or len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 786432):
        raise ValueError
    return deepcopy(data)


def _navigation_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
        raise EdgeBridgeError("agent_browser_url_invalid")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        parsed.port
    except ValueError:
        raise EdgeBridgeError("agent_browser_url_invalid") from None
    return value


def _origin(value):
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        host = f"[{host}]" if ":" in host else host
        port = parsed.port
        authority = host if port is None or (parsed.scheme, port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
        return urlunsplit((parsed.scheme, authority, "", "", ""))
    except ValueError:
        return ""


def _parse_snapshot(tree, refs, *, max_bytes):
    """Parse the complete supported safe grammar, never a first-N prefix."""
    for key, value in refs.items():
        if (not isinstance(key, str) or not _REF.fullmatch(key) or not isinstance(value, dict)
                or not isinstance(value.get("role"), str) or not _ROLE.fullmatch(value["role"])
                or not isinstance(value.get("name"), str)):
            raise _SnapshotInvalid("structured_ref")
    if tree.strip() in {"", "(empty page)", "(no interactive elements)"}:
        return []
    nodes, ancestors, seen = [], [], set()
    projection_bytes = 0
    decoder = json.JSONDecoder()
    for line in tree.splitlines():
        match = re.match(r"^( *)- ([A-Za-z][A-Za-z0-9_-]*)(.*)$", line)
        if not match or len(match[1]) % 2:
            raise _SnapshotInvalid("line_grammar")
        depth, role, rest = len(match[1]) // 2, match[2], match[3]
        name = ""
        if rest.startswith(' "'):
            try:
                name, end = decoder.raw_decode(rest[1:])
                rest = rest[end + 1:]
            except ValueError:
                raise _SnapshotInvalid("name_json") from None
        if not isinstance(name, str):
            raise _SnapshotInvalid("name_type")
        attrs = []
        if rest:
            if not rest.startswith(" [") or not rest.endswith("]"):
                # Value suffixes are deliberately not projected or tolerated.
                raise _SnapshotInvalid("attribute_grammar")
            attrs = rest[2:-1].split(", ")
            if len(set(attrs)) != len(attrs) or any(not _ATTR.fullmatch(attr) for attr in attrs):
                raise _SnapshotInvalid("attribute_value")
        helper_refs = [attr[4:] for attr in attrs if attr.startswith("ref=")]
        if len(helper_refs) > 1:
            raise _SnapshotInvalid("multiple_refs")
        ref = helper_refs[0] if helper_refs else None
        if ref is not None:
            value = refs.get(ref)
            if value is None:
                raise _SnapshotInvalid("unknown_ref")
            if ref in seen:
                raise _SnapshotInvalid("repeated_ref")
            if value["role"] != role:
                raise _SnapshotInvalid("ref_role_mismatch")
            if value["name"].translate(_ZERO_WIDTH) != name:
                raise _SnapshotInvalid("ref_name_mismatch")
            seen.add(ref)
            name = value["name"].translate(_ZERO_WIDTH)
        while ancestors and ancestors[-1]["depth"] >= depth:
            ancestors.pop()
        context = " / ".join(node["label"] for node in ancestors if node["label"])
        try:
            projection_bytes += len(context.encode("utf-8")) + len(name.encode("utf-8"))
        except UnicodeError:
            raise _SnapshotInvalid("label_unicode") from None
        if projection_bytes > max_bytes:
            raise _SnapshotBudget
        node = {"depth": depth, "role": role, "label": name, "ref": ref,
                "disabled": "disabled" in attrs, "context": context}
        nodes.append(node)
        ancestors.append(node)
    return nodes
