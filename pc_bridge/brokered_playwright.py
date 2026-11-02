"""Playwright-protocol client for the Gateway's Browser.bind Edge broker.

This module accepts only the broker's ephemeral loopback WebSocket endpoint.
It never reads Edge profile files, resolves DevTools endpoints, or creates a
direct CDP connection. Page access is limited to the already-bound default
context exposed by the broker.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import math
import os
import re
import secrets
import threading
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from .edge_cdp import BrokerWebSocketEndpoint, EdgeCdpError

_REF = re.compile(r"e[1-9][0-9]{0,19}\Z")
_TARGET = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ROLE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ATTR = re.compile(r"(?:level=[0-9]+|checked=(?:true|false|mixed)|expanded=(?:true|false)|selected|disabled|required)\Z")
_SNAPSHOT_ROLES = frozenset({
    "alert", "banner", "button", "cell", "checkbox", "combobox", "complementary",
    "contentinfo", "document", "generic", "grid", "gridcell", "group", "heading",
    "link", "list", "listitem", "main", "menuitem", "menuitemcheckbox", "menuitemradio",
    "navigation", "option", "paragraph", "radio", "region", "row", "searchbox", "switch",
    "tab", "table", "textbox", "treeitem",
})
_CROWDWORKS_ORIGINS = frozenset({"https://crowdworks.jp", "https://www.crowdworks.jp"})
_CROWDWORKS_HREF = re.compile(r"https://crowdworks\.jp/public/jobs/[0-9]+\Z")
_CROWDWORKS_TEXT_BYTES = 131_072
_CROWDWORKS_CONTEXT_BYTES = 16_000
_CROWDWORKS_TOTAL_BYTES = 524_288


class BrokeredPlaywrightRuntime:
    """Synchronous command adapter backed by one owned Playwright client thread."""

    def __init__(self, endpoint: str, *, playwright_factory=None, session_id: str | None = None,
                 on_disconnect=None):
        if any(key.casefold() == "agent_browser_auto_connect" for key in os.environ):
            raise ValueError("direct_edge_cdp_forbidden")
        if on_disconnect is not None and not callable(on_disconnect):
            raise ValueError("edge_broker_disconnect_callback_invalid")
        try:
            self.endpoint = BrokerWebSocketEndpoint(endpoint)
        except EdgeCdpError:
            raise ValueError("edge_broker_endpoint_invalid") from None
        self._on_disconnect = on_disconnect
        self.session_id = session_id or ("broker-" + uuid4().hex)
        self.attach_target = None
        self.discover_existing = True
        self._playwright_factory = playwright_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pc-bridge-broker")
        self._closed = False
        self._close_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_operations = 0
        self._quiescent = threading.Event()
        self._quiescent.set()
        self._disconnect_lock = threading.Lock()
        self._disconnect_notified = False
        self._intentional_close = False
        self._active_operation = "other"
        self._browser = None
        self._context = None
        self._playwright = None
        self._pages: dict[str, object] = {}
        self._page_ids: dict[int, str] = {}
        self._selected_target: str | None = None
        self._references: dict[str, dict] = {}
        self._last_command_diagnostic = {
            "exit_code": None, "response_code": None, "stderr_summary": "not_run",
        }

    def __repr__(self):
        # Do not expose the ephemeral broker endpoint through object diagnostics.
        return "<BrokeredPlaywrightRuntime>"

    @property
    def last_command_diagnostic(self):
        return dict(self._last_command_diagnostic)

    @property
    def connection_lost(self):
        with self._disconnect_lock:
            return self._disconnect_notified and not self._intentional_close

    def preflight(self, *, timeout_seconds=10, cancel_event=None):
        del timeout_seconds, cancel_event
        from .agent_browser_adapter import REQUIRED_CAPABILITIES

        return {"bridgeProtocol": 1, "capabilities": sorted(REQUIRED_CAPABILITIES)}

    def set_attach_target(self, target: str):
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise ValueError("agent_browser_target_invalid")
        self.attach_target = target

    def run(self, args, *, stdin_bytes=None, timeout_seconds=10, cancel_event=None,
            max_output_bytes=1_048_576):
        del max_output_bytes
        if self._closed:
            return self._failure("agent_browser_runtime_closed")
        if cancel_event is not None and cancel_event.is_set():
            return self._failure("agent_browser_cancelled")
        try:
            future = self._submit(self._dispatch, tuple(args), stdin_bytes, timeout_seconds)
        except RuntimeError:
            return self._failure("agent_browser_runtime_closed")
        try:
            wait_timeout = timeout_seconds if tuple(args) == ("health-check",) else timeout_seconds + 1
            response = future.result(timeout=wait_timeout)
        except FutureTimeout:
            future.cancel()
            is_health_check = tuple(args) == ("health-check",)
            if is_health_check:
                # Version probes are read-only. Let one timed-out probe settle;
                # the Bridge debounces repeated timeouts before aborting this
                # client binding and starting recovery.
                code = "edge_broker_probe_timeout"
                self._last_command_diagnostic["response_code"] = code
                return self._failure(code, executed=False)
            code = "outcome_unknown"
            self._last_command_diagnostic["response_code"] = code
            self._notify_disconnect("edge_broker_operation_outcome_unknown")
            return {"success": False, "code": code, "data": {"executed": False}}
        except Exception:
            is_health_check = tuple(args) == ("health-check",)
            code = "edge_broker_browser_disconnected" if is_health_check else "edge_broker_client_command_failed"
            self._last_command_diagnostic["response_code"] = code
            if is_health_check:
                self._notify_disconnect(code)
            return self._failure(code, executed=False)
        code = response.get("code") if isinstance(response, dict) else None
        self._last_command_diagnostic["response_code"] = code if isinstance(code, str) else None
        return response

    def stop(self):
        with self._close_lock:
            if self._closed:
                return True
            self._closed = True
            self._mark_intentional_close()
        try:
            future = self._submit(self._close_on_worker)
            future.result(timeout=5)
            self._executor.shutdown(wait=True, cancel_futures=False)
            return True
        except Exception:
            # A blocked operation may still be running after the caller times
            # out. Keep the queued close task so it runs immediately after that
            # operation settles; cancelling it would leak the broker client.
            self._executor.shutdown(wait=False, cancel_futures=False)
            return False

    def _submit(self, callback, *args):
        """Track work that may outlive a caller timeout so reconnect can wait."""
        with self._pending_lock:
            self._pending_operations += 1
            self._quiescent.clear()
        try:
            future = self._executor.submit(callback, *args)
        except Exception:
            self._finish_operation()
            raise
        future.add_done_callback(lambda _future: self._finish_operation())
        return future

    def _finish_operation(self):
        with self._pending_lock:
            self._pending_operations = max(0, self._pending_operations - 1)
            if self._pending_operations == 0:
                self._quiescent.set()

    def wait_quiescent(self, timeout_seconds=0.0):
        """Return true only after every submitted browser operation has ended."""
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds < 0):
            return False
        return self._quiescent.wait(timeout_seconds)

    def _dispatch(self, args, stdin_bytes, timeout_seconds):
        with self._disconnect_lock:
            self._active_operation = self._operation_for(args, stdin_bytes)
        try:
            if args == ("health-check",):
                return self._health_check()
            if args == ("connect", self.endpoint.url):
                return self._connect(timeout_seconds)
            if args == ("bridge-command", "--stdin"):
                try:
                    envelope = json.loads(stdin_bytes.decode("utf-8"))
                except Exception:
                    return self._failure("invalid_arguments")
                if (not isinstance(envelope, dict) or set(envelope) != {"argv"}
                        or not isinstance(envelope["argv"], list)
                        or any(not isinstance(item, str) for item in envelope["argv"])):
                    return self._failure("invalid_arguments")
                return self._command(tuple(envelope["argv"]), timeout_seconds)
            if args == ("tab", "list"):
                return self._tab_list_response()
            if args[:1] == ("snapshot",):
                return self._command(tuple(args), timeout_seconds)
            if len(args) == 2 and args[0] == "tab":
                return self._select(args[1])
            return self._failure("invalid_arguments")
        finally:
            with self._disconnect_lock:
                self._active_operation = "other"

    @staticmethod
    def _operation_for(args, stdin_bytes):
        if args == ("health-check",):
            return "health_check"
        if len(args) == 2 and args[0] == "connect":
            return "connect"
        argv = args
        if args == ("bridge-command", "--stdin"):
            try:
                envelope = json.loads(stdin_bytes.decode("utf-8"))
                argv = tuple(envelope.get("argv", ())) if isinstance(envelope, dict) else ()
            except Exception:
                argv = ()
        if argv == ("tab", "list") or argv == ("tab", "list", "--local-labels"):
            return "target_list"
        if argv[:1] == ("tab",):
            return "target_select"
        if argv[:1] == ("open-target",):
            return "target_open"
        if argv[:1] in {("navigate",), ("back",)}:
            return "navigate"
        if argv[:1] in {("verify",), ("resolve_tab",)}:
            return "verify"
        if argv[:1] in {("snapshot",), ("crowdworks-inspect",), ("observe",)}:
            return "read"
        if argv[:1] in {("act",), ("click",), ("type",), ("fill",), ("select",),
                        ("check",), ("uncheck",), ("press",)}:
            return "action"
        return "other"

    def abort_pending_health_check(self):
        """Abort only a stalled read-only version probe after Bridge debounce."""
        with self._disconnect_lock:
            if self._closed or self._active_operation != "health_check":
                return False
            # The Bridge has already decided this probe is persistently
            # unresponsive and will report the fixed recovery transition.
            self._intentional_close = True
        return self._interrupt_browser_bind_transport()

    def _health_check(self):
        """Round-trip a harmless browser-version request through the bound broker."""
        browser = self._browser
        if browser is None:
            self._notify_disconnect("edge_broker_browser_disconnected")
            return self._failure("edge_broker_browser_disconnected", executed=False)
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected):
            try:
                if is_connected() is not True:
                    self._notify_disconnect("edge_broker_browser_disconnected")
                    return self._failure("edge_broker_browser_disconnected", executed=False)
            except Exception:
                self._notify_disconnect("edge_broker_browser_disconnected")
                return self._failure("edge_broker_browser_disconnected", executed=False)

        session = None
        healthy = False
        try:
            create_session = getattr(browser, "new_browser_cdp_session", None)
            if not callable(create_session):
                raise RuntimeError("health_probe_unavailable")
            session = create_session()
            response = session.send("Browser.getVersion")
            healthy = isinstance(response, dict) and bool(response)
        except Exception:
            healthy = False
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    healthy = False
        if not healthy:
            self._notify_disconnect("edge_broker_browser_disconnected")
            return self._failure("edge_broker_browser_disconnected", executed=False)
        return self._success({"reachable": True})

    def _interrupt_browser_bind_transport(self):
        """Stop the timed-out client binding on its Playwright loop, not Edge."""
        try:
            implementation = getattr(self._playwright, "_impl_obj", None)
            connection = getattr(implementation, "_connection", None)
            loop = getattr(connection, "_loop", None)
            children = tuple(getattr(connection, "_child_ws_connections", ()))
            transports = tuple(
                transport for transport in
                (getattr(child, "_transport", None) for child in children)
                if callable(getattr(transport, "request_stop", None))
            )
            call_soon_threadsafe = getattr(loop, "call_soon_threadsafe", None)
            if not transports or not callable(call_soon_threadsafe):
                return False
            stopped = threading.Event()
            stop_results = []

            def stop_bindings():
                succeeded = True
                for transport in transports:
                    try:
                        transport.request_stop()
                    except Exception:
                        succeeded = False
                stop_results.append(succeeded)
                stopped.set()

            call_soon_threadsafe(stop_bindings)
            return stopped.wait(0.5) and stop_results == [True]
        except Exception:
            return False

    def _mark_intentional_close(self):
        with self._disconnect_lock:
            self._intentional_close = True

    def _notify_disconnect(self, code):
        if code not in {
            "edge_broker_browser_disconnected", "edge_broker_browser_context_closed",
            "edge_broker_operation_outcome_unknown",
        }:
            return
        with self._disconnect_lock:
            if self._intentional_close or self._disconnect_notified:
                return
            self._disconnect_notified = True
            callback = self._on_disconnect
            operation = self._active_operation
        if callback is not None:
            try:
                callback(code, operation)
            except Exception:
                # A monitor callback must never leak transport details or destabilize Playwright.
                pass

    def _install_event_monitor(self, emitter, event, code):
        register = getattr(emitter, "on", None)
        if not callable(register):
            return False
        try:
            register(event, lambda *_args, **_kwargs: self._notify_disconnect(code))
        except Exception:
            return False
        return True

    @staticmethod
    def _failure(code, *, executed=False):
        return {"success": False, "code": code, "data": {"executed": executed}}

    @staticmethod
    def _success(data):
        return {"success": True, "data": data}

    def _connect(self, timeout_seconds):
        if self._browser is not None:
            return self._failure("connection_blocked")
        try:
            if self._playwright_factory is None:
                from playwright.sync_api import sync_playwright
                playwright_factory = sync_playwright
            else:
                playwright_factory = self._playwright_factory
            manager = playwright_factory()
            self._playwright = manager.start() if callable(getattr(manager, "start", None)) else manager
            self._browser = self._playwright.chromium.connect(
                self.endpoint.url, timeout=max(1, int(timeout_seconds * 1000)))
            if not self._install_event_monitor(
                    self._browser, "disconnected", "edge_broker_browser_disconnected"):
                self._close_on_worker()
                return self._failure("edge_broker_event_monitor_unavailable")
            is_connected = getattr(self._browser, "is_connected", None)
            if callable(is_connected) and not is_connected():
                self._notify_disconnect("edge_broker_browser_disconnected")
                self._close_on_worker()
                return self._failure("edge_broker_browser_disconnected")
            contexts = self._browser.contexts
            if not contexts:
                self._close_on_worker()
                return self._failure("edge_broker_default_context_unavailable")
            self._context = contexts[0]
            if not self._install_event_monitor(
                    self._context, "close", "edge_broker_browser_context_closed"):
                self._close_on_worker()
                return self._failure("edge_broker_event_monitor_unavailable")
            context_is_closed = getattr(self._context, "is_closed", None)
            if callable(context_is_closed) and context_is_closed():
                self._notify_disconnect("edge_broker_browser_context_closed")
                self._close_on_worker()
                return self._failure("edge_broker_browser_context_closed")
            if callable(is_connected) and not is_connected():
                self._notify_disconnect("edge_broker_browser_disconnected")
                self._close_on_worker()
                return self._failure("edge_broker_browser_disconnected")
            return self._success({"completed": True})
        except Exception:
            self._close_on_worker()
            return self._failure("edge_broker_client_connect_failed")

    def _close_on_worker(self):
        self._mark_intentional_close()
        browser, playwright = self._browser, self._playwright
        self._browser = self._context = self._playwright = None
        self._pages.clear()
        self._page_ids.clear()
        self._references.clear()
        self._selected_target = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _page_target_id(self, page):
        key = id(page)
        target = self._page_ids.get(key)
        if target is None:
            target = "p_" + secrets.token_hex(12)
            self._page_ids[key] = target
        self._pages[target] = page
        return target

    @staticmethod
    def _origin(value):
        if not isinstance(value, str) or not value:
            return ""
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ""
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            port = parsed.port
            authority = host if port is None or (parsed.scheme, port) in {
                ("http", 80), ("https", 443),
            } else f"{host}:{port}"
            return urlunsplit((parsed.scheme, authority, "", "", ""))
        except ValueError:
            return ""

    def _current_pages(self):
        if self._context is None:
            raise RuntimeError("connection_required")
        pages = []
        live_ids = set()
        for page in tuple(self._context.pages):
            if page.is_closed():
                continue
            target = self._page_target_id(page)
            live_ids.add(target)
            pages.append({
                "targetId": target,
                "type": "page",
                "active": target == self._selected_target,
                "origin": self._origin(page.url),
                "title": "",
            })
        self._pages = {key: self._pages[key] for key in live_ids}
        self._page_ids = {id(page): key for key, page in self._pages.items()}
        return pages

    def _tab_list_response(self):
        try:
            return self._success({"tabs": self._current_pages()})
        except RuntimeError:
            return self._failure("connection_required")
        except Exception:
            return self._failure("edge_broker_client_command_failed")

    def _open_target(self, url, timeout_seconds):
        """Open one fixed CrowdWorks origin in the already-bound broker context."""
        if self._context is None:
            return self._failure("connection_required")
        if (not isinstance(url, str)
                or url not in {"https://crowdworks.jp/", "https://www.crowdworks.jp/"}):
            return self._failure("agent_browser_target_open_scope_invalid", executed=False)
        try:
            origin = self._origin(url)
            existing = sum(
                1 for page in tuple(self._context.pages)
                if not page.is_closed() and self._origin(page.url) == origin
            )
            if existing:
                return self._success({"opened": False, "match_count": existing})
            page = self._context.new_page()
            target = self._page_target_id(page)
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=max(1, int(timeout_seconds * 1000)))
            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                self._pages.pop(target, None)
                self._page_ids.pop(id(page), None)
                return self._failure("agent_browser_target_open_failed", executed=True)
            if self._origin(page.url) != origin:
                try:
                    page.close()
                except Exception:
                    pass
                self._pages.pop(target, None)
                self._page_ids.pop(id(page), None)
                return self._failure("agent_browser_target_open_failed", executed=True)
            return self._success({"opened": True, "match_count": 0})
        except Exception:
            return self._failure("agent_browser_target_open_failed", executed=None)

    def _select(self, target):
        if self._context is None:
            return self._failure("connection_required")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            return self._failure("invalid_arguments")
        try:
            pages = self._current_pages()
            if not any(row["targetId"] == target for row in pages):
                return self._failure("target_unavailable")
            self._selected_target = target
            return self._success({"completed": True})
        except Exception:
            return self._failure("edge_broker_client_command_failed")

    def _selected_page(self):
        if self._context is None or self._selected_target is None:
            raise RuntimeError("connection_required")
        page = self._pages.get(self._selected_target)
        if page is None or page.is_closed():
            raise RuntimeError("target_unavailable")
        return page

    def _command(self, args, timeout_seconds):
        if self._context is None:
            return self._failure("connection_required")
        try:
            if args == ("tab", "list", "--local-labels"):
                return self._tab_list_response()
            if len(args) == 2 and args[0] == "open-target":
                return self._open_target(args[1], timeout_seconds)
            if args[:1] == ("snapshot",):
                return self._snapshot(args[1:])
            if args == ("verify",) or args[:1] == ("verify",):
                return self._verify(args[1:])
            if args == ("crowdworks-inspect",):
                return self._crowdworks_inspect()
            return self._page_command(args, timeout_seconds)
        except RuntimeError as exc:
            code = str(exc)
            return self._failure(code if code in {
                "connection_required", "target_unavailable", "SAFE_SNAPSHOT_UNAVAILABLE",
                "CROWDWORKS_SCOPE_UNSUPPORTED", "CROWDWORKS_SCOPE_OVERFLOW",
            } else "edge_broker_client_command_failed")
        except Exception:
            return self._failure("outcome_unknown", executed=None)

    def _snapshot(self, args):
        page = self._selected_page()
        selector, depth = None, None
        index = 0
        while index < len(args):
            item = args[index]
            if item in {"--interactive", "--redact-editable-values"}:
                index += 1
                continue
            if item in {"--selector", "--depth"} and index + 1 < len(args):
                value = args[index + 1]
                if item == "--selector":
                    if not value or len(value) > 4096 or value.startswith("--") or "\0" in value:
                        return self._failure("invalid_arguments")
                    selector = value
                else:
                    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 100:
                        return self._failure("invalid_arguments")
                    depth = int(value)
                index += 2
                continue
            return self._failure("invalid_arguments")
        locator = page.locator(selector) if selector else page.locator("body")
        raw = locator.aria_snapshot(timeout=5000, depth=depth)
        tree, refs, reference_map = self._project_aria_snapshot(raw, selector)
        self._references = reference_map
        return self._success({"snapshot": tree, "refs": refs, "origin": self._origin(page.url)})

    @staticmethod
    def _project_aria_snapshot(raw, selector):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 262_144:
            raise RuntimeError("SAFE_SNAPSHOT_UNAVAILABLE")
        lines, refs, reference_map = [], {}, {}
        occurrence: dict[tuple[str, str], int] = {}
        decoder = json.JSONDecoder()
        for source_line in raw.splitlines():
            match = re.match(r"^( *)- (.+)$", source_line)
            if not match:
                continue
            body = match[2]
            role_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]{0,63})(.*)$", body)
            if not role_match:
                continue
            role, rest = role_match.groups()
            if role not in _SNAPSHOT_ROLES:
                continue
            name = ""
            if rest.startswith(' "'):
                try:
                    name, end = decoder.raw_decode(rest[1:])
                except ValueError:
                    continue
                if not isinstance(name, str):
                    continue
                rest = rest[end + 1:]
            attrs = []
            if rest:
                if not rest.startswith(" [") or not rest.endswith("]"):
                    continue
                for attr in rest[2:-1].split(", "):
                    if attr == "checked":
                        attr = "checked=true"
                    if _ATTR.fullmatch(attr):
                        attrs.append(attr)
            ref = f"e{len(refs) + 1}"
            if not _REF.fullmatch(ref):
                raise RuntimeError("SAFE_SNAPSHOT_UNAVAILABLE")
            refs[ref] = {"role": role, "name": name}
            key = (role, name)
            nth = occurrence.get(key, 0)
            occurrence[key] = nth + 1
            reference_map[ref] = {"role": role, "name": name, "nth": nth, "selector": selector}
            indent = " " * (len(match[1]) // 2 * 2)
            encoded_name = " " + json.dumps(name, ensure_ascii=False) if name else ""
            suffix = " [" + ", ".join([*attrs, f"ref={ref}"]) + "]"
            lines.append(f"{indent}- {role}{encoded_name}{suffix}")
        return "\n".join(lines), refs, reference_map

    def _locator_for_ref(self, ref):
        data = self._references.get(ref)
        if not isinstance(data, dict) or not _REF.fullmatch(ref):
            raise RuntimeError("STALE_REF")
        page = self._selected_page()
        root = page.locator(data["selector"]) if data.get("selector") else page
        if data["name"]:
            locator = root.get_by_role(data["role"], name=data["name"], exact=True)
        else:
            locator = root.get_by_role(data["role"], name="", exact=True)
        if locator.count() <= data["nth"]:
            raise RuntimeError("STALE_REF")
        return locator.nth(data["nth"])

    def _page_command(self, args, timeout_seconds):
        page = self._selected_page()
        if not args:
            return self._failure("invalid_arguments")
        action = args[0]
        if action == "open" and len(args) == 2:
            page.goto(args[1], wait_until="domcontentloaded", timeout=max(1, int(timeout_seconds * 1000)))
        elif action == "back" and len(args) == 1:
            page.go_back(wait_until="domcontentloaded", timeout=max(1, int(timeout_seconds * 1000)))
        elif action == "scroll" and len(args) == 2 and args[1] in {"up", "down"}:
            page.mouse.wheel(0, -600 if args[1] == "up" else 600)
        elif action in {"click", "fill", "check", "uncheck", "select"} and len(args) in {2, 3}:
            ref = args[1][1:] if args[1].startswith("@") else ""
            locator = self._locator_for_ref(ref)
            if locator.is_disabled() or not locator.is_visible():
                return self._failure("STALE_REF")
            timeout = max(1, int(timeout_seconds * 1000))
            if action == "click" and len(args) == 2:
                locator.click(timeout=timeout)
            elif action == "fill" and len(args) == 3:
                locator.fill(args[2], timeout=timeout)
            elif action == "check" and len(args) == 2:
                locator.check(timeout=timeout)
            elif action == "uncheck" and len(args) == 2:
                locator.uncheck(timeout=timeout)
            elif action == "select" and len(args) == 3:
                locator.select_option(label=args[2], timeout=timeout)
            else:
                return self._failure("invalid_arguments")
        else:
            return self._failure("invalid_arguments")
        return self._success({"completed": True, "origin": self._origin(page.url)})

    def _verify(self, args):
        page = self._selected_page()
        if len(args) % 2:
            return self._failure("invalid_arguments")
        params = {}
        for key, value in zip(args[::2], args[1::2]):
            if key not in {"--expected-url", "--expected-text", "--ref", "--expected-value"} or key in params:
                return self._failure("invalid_arguments")
            params[key] = value
        if not params:
            return self._failure("invalid_arguments")
        matched = True
        if "--expected-url" in params:
            matched &= page.url == params["--expected-url"]
        if "--expected-text" in params:
            matched &= page.get_by_text(params["--expected-text"], exact=False).count() > 0
        if "--expected-value" in params:
            locator = self._locator_for_ref(params.get("--ref", ""))
            matched &= locator.input_value() == params["--expected-value"]
        elif "--ref" in params:
            return self._failure("invalid_arguments")
        return self._success({"matched": bool(matched)})

    def _crowdworks_inspect(self):
        page = self._selected_page()
        origin = self._origin(page.url)
        if origin not in _CROWDWORKS_ORIGINS:
            return self._failure("CROWDWORKS_SCOPE_UNSUPPORTED")
        main = page.locator("main")
        main_count = main.count()
        if main_count > 1:
            return self._failure("CROWDWORKS_SCOPE_UNSUPPORTED")
        text_locator = main if main_count == 1 else page.locator("body")
        text = text_locator.inner_text(timeout=5000).strip()
        try:
            text_bytes = len(text.encode("utf-8"))
        except UnicodeError:
            return self._failure("CROWDWORKS_SCOPE_UNSUPPORTED")
        if text_bytes > _CROWDWORKS_TEXT_BYTES:
            return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
        # One atomic read of every anchor: a page that re-renders between count() and
        # nth() would otherwise make a vanished anchor wait out Playwright's 30 s timeout.
        scan = page.locator("a[href]").evaluate_all("""elements => {
          const selector = 'tr,[role="row"],li,[role="listitem"],article';
          const jobPath = /^https:[/][/]crowdworks[.]jp[/]public[/]jobs[/][0-9]+$/;
          const rows = [];
          for (const element of elements) {
            const raw = element.getAttribute('href');
            if (typeof raw !== 'string') continue;
            let resolved;
            try {
              const url = new URL(raw, location.href);
              resolved = url.protocol + '//' + url.host + url.pathname;
            } catch (error) { continue; }
            if (!jobPath.test(resolved)) continue;
            const label = element.innerText || '';
            let context = label;
            try {
              let node = element;
              context = null;
              for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                if (node.matches(selector)) { context = node.innerText || node.textContent || ''; break; }
              }
              if (context === null) {
                context = element.parentElement?.innerText || element.parentElement?.textContent || element.innerText || '';
              }
            } catch (error) { context = label; }
            rows.push({href: raw, label, context});
          }
          return {count: elements.length, rows};
        }""")
        if not isinstance(scan, dict) or not isinstance(scan.get("rows"), list) or type(scan.get("count")) is not int:
            return self._failure("CROWDWORKS_SCOPE_UNSUPPORTED")
        if scan["count"] > 4096:
            return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
        candidates = {}
        total_bytes = text_bytes
        for row in scan["rows"]:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("href", "label", "context")):
                continue
            try:
                href = urljoin(page.url, row["href"])
                parsed = urlsplit(href)
                href = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except ValueError:
                continue
            if not _CROWDWORKS_HREF.fullmatch(href):
                continue
            label = row["label"].strip()
            if len(label.encode("utf-8")) > 4096:
                return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
            context = row["context"].strip()
            if len(context.encode("utf-8")) > _CROWDWORKS_CONTEXT_BYTES:
                return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
            total_bytes += len(href.encode("utf-8")) + len(label.encode("utf-8")) + len(context.encode("utf-8"))
            if total_bytes > _CROWDWORKS_TOTAL_BYTES:
                return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
            key = f"cw_{len(candidates)}"
            candidates[key] = {"href": href, "label": label, "context": context}
            if len(candidates) > 200:
                return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
        projection = {
            "origin": origin,
            "candidates": candidates,
            "text": text,
            "coverage": {
                "complete": True, "frame_scope": "main", "shadow_scope": "none",
                "candidate_count": len(candidates), "truncated": False, "unscanned": False,
            },
        }
        if len(json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 786_432:
            return self._failure("CROWDWORKS_SCOPE_OVERFLOW")
        return self._success(projection)
