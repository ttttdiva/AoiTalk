"""Private stdio driver whose Node/browser descendants are one owned Job.

No debugger endpoint is exposed or discovered. Runtime construction follows an
explicit local profile selection. The optional fixture input is a test-only
constructor seam and is never accepted by Bridge config or WSS commands.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
from uuid import uuid4

from .browser_profiles import BrowserProfileError, _local_path
from .edge import EdgeBridgeError
from .paths import extension_source, home
from .profile_security import protect_directory
from .windows_helper_job import WindowsHelperJob
from .browser_commands import DEDICATED_BROWSER_COMMANDS

MAX_BYTES = 16 * 1024 * 1024
PLAYWRIGHT_VERSION = "1.63.0"
DEDICATED_COMMANDS = DEDICATED_BROWSER_COMMANDS


def dedicated_runtime_available():
    """Old/frozen media-only bundles must never advertise this capability."""
    try:
        import win32crypt
        if not callable(win32crypt.CryptProtectData) or not callable(win32crypt.CryptUnprotectData):
            return False
        _, _, script = packaged_runtime()
        manifest = json.loads(script.with_name("dedicated-manifest.json").read_text(encoding="utf-8"))
        provenance = json.loads(script.with_name("jev-provenance.json").read_text(encoding="utf-8"))
        if (provenance.get("upstream", {}).get("commit") != "d8f1ce0c6d09a8cbe43110ecbe884ecb842083ec"
                or any(provenance.get(key) != hashlib.sha256(script.with_name(name).read_bytes()).hexdigest()
                       for key, name in (("bundle_sha256", "jev-lib.cjs"), ("patch_sha256", "jev-browser.patch"),
                                         ("notices_sha256", "JEV-THIRD-PARTY-NOTICES.txt")))):
            return False
        return manifest == {"protocol": 1, "commands": sorted(DEDICATED_COMMANDS),
                            "driver_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                            "jev_lib_sha256": hashlib.sha256(script.with_name("jev-lib.cjs").read_bytes()).hexdigest(),
                            "jev_provenance_sha256": hashlib.sha256(script.with_name("jev-provenance.json").read_bytes()).hexdigest(),
                            "material_state_guard": 3,
                            "controller_sha256": {
                                name: hashlib.sha256((extension_source() / name).read_bytes()).hexdigest()
                                for name in ("background.js", "content.js")}}
    except (OSError, ValueError, EdgeBridgeError, ImportError):
        return False


def packaged_runtime():
    import playwright
    root = _local_path(Path(playwright.__file__).parent / "driver")
    node, package = _local_path(root / "node.exe"), _local_path(root / "package")
    try:
        if (json.loads((package / "package.json").read_text(encoding="utf-8"))["version"] != PLAYWRIGHT_VERSION
                or not node.is_file()):
            raise ValueError()
    except (OSError, ValueError, KeyError):
        raise EdgeBridgeError("profile_driver_bundle_unavailable") from None
    resources = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else home()
    script = _local_path(resources / "resources" / "browser-profile-driver" / "driver.cjs")
    if not script.is_file():
        raise EdgeBridgeError("profile_driver_bundle_unavailable")
    return node, package, script


class ProfileBrowserDriver:
    def __init__(self, lease, *, channel="msedge", headless=False, fixture=None, runtime_root=None):
        if os.name != "nt" or channel not in {"msedge", "chrome"} or type(headless) is not bool:
            raise EdgeBridgeError("profile_driver_config_invalid")
        lease.verify()
        self.lease = lease
        self._dedicated = lease.scope.service == "browser"
        if self._dedicated and not dedicated_runtime_available():
            raise EdgeBridgeError("profile_driver_dedicated_bundle_unavailable")
        self.profile_id, self.connection_epoch = lease.profile_id, lease.connection_epoch
        self.user_data_dir, self.browser_context = lease.user_data_dir, dict(lease.context)
        self._lock, self._cancelled = threading.RLock(), threading.Event()
        self._closed, self._browser_closed, self._verified_cleanup = False, False, False
        self._replies, self._counter = queue.Queue(), 0
        self._process = self._job = None
        self._fixture = fixture is not None
        self._diagnostic = bytearray()
        node, package, script = packaged_runtime()
        root = _local_path(runtime_root if runtime_root is not None else home() / "cache" / "pc-bridge-profiles")
        root.mkdir(parents=True, exist_ok=True)
        protect_directory(root)
        self._runtime_dir = _local_path(root / ("session-" + uuid4().hex))
        self._runtime_dir.mkdir()
        protect_directory(self._runtime_dir)
        for name in ("tmp", "home"):
            (self._runtime_dir / name).mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)",
                                          "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH"}}
        environment.update(TEMP=str(self._runtime_dir / "tmp"), TMP=str(self._runtime_dir / "tmp"),
            TMPDIR=str(self._runtime_dir / "tmp"))
        if self._fixture:
            environment["DEBUG"] = "pw:browser"
        self._job_name = "Local\\AoiTalk-Profile-" + lease.profile_id + "-" + uuid4().hex
        try:
            self._job = WindowsHelperJob(name=self._job_name)
            lease.start_runtime(self._job_name)
            self._process = self._job.spawn((str(node), str(script)), cwd=self._runtime_dir, environment=environment)
            lease.start_runtime(self._job_name, self._job.snapshot_identities())
            self._reader = threading.Thread(target=self._read, daemon=True, name="profile-driver-output")
            self._stderr = threading.Thread(target=self._drain_stderr, daemon=True, name="profile-driver-errors")
            self._reader.start()
            self._stderr.start()
            ready = self._rpc("initialize", {"user_data_dir": str(lease.user_data_dir), "state_dir": str(lease.directory),
                "resource_dir": str(extension_source()), "package_dir": str(package),
                "browser_context": self.browser_context, "service": lease.scope.service, "channel": channel,
                "headless": headless, "fixture": str(_local_path(fixture)) if fixture is not None else None}, timeout=30)
            if ready != {"ready": True, "version": 1, "browser_context": self.browser_context}:
                raise EdgeBridgeError("profile_driver_context_unverified")
            lease.start_runtime(self._job_name, self._job.snapshot_identities())
        except Exception as exc:
            self.close()
            if self._fixture:
                Path(fixture).with_suffix(".driver-diagnostic.txt").write_bytes(self._diagnostic)
            if isinstance(exc, EdgeBridgeError):
                raise
            raise EdgeBridgeError("profile_driver_start_failed") from None

    def _read(self):
        buffer = b""
        try:
            while True:
                data = self._process.read_output(self._process.stdout, 65536)
                if not data:
                    break
                buffer += data
                if len(buffer) > MAX_BYTES:
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    value = json.loads(line)
                    if value == {"event": "browser_closed"}:
                        self._browser_closed = True
                    else:
                        self._replies.put(value)
        except Exception:
            pass
        finally:
            self._browser_closed = True
            self._replies.put(None)

    def _drain_stderr(self):
        # Drain without storing page/credential/path-bearing SDK diagnostics.
        try:
            while data := self._process.read_output(self._process.stderr, 65536):
                if self._fixture and len(self._diagnostic) < 16384:
                    self._diagnostic.extend(data[:16384 - len(self._diagnostic)])
        except Exception:
            pass

    def _rpc(self, command, params, *, timeout=30):
        with self._lock:
            if self._closed or self._cancelled.is_set() or self._process is None:
                raise EdgeBridgeError("profile_driver_closed")
            self._counter += 1
            message = json.dumps({"id": self._counter, "command": command, "params": params},
                                 ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if len(message) > MAX_BYTES:
                raise EdgeBridgeError("profile_driver_input_limit")
            try:
                self._process.stdin.write(message)
                deadline = time.monotonic() + timeout
                answered_events = set()
                while True:
                    reply = self._replies.get(timeout=max(0, deadline - time.monotonic()))
                    # A running jev_run asks the host for judgments and for the
                    # pre-mutation gate; answer here (fail closed) and keep waiting.
                    if isinstance(reply, dict) and reply.get("event") in {"jev_ask", "jev_gate", "jev_type"}:
                        if (command != "jev_run" or reply.get("run_id") != params.get("run_id")
                                or not isinstance(reply.get("id"), str) or reply["id"] in answered_events):
                            self.cancel()
                            raise EdgeBridgeError("profile_driver_jev_reply_invalid")
                        answered_events.add(reply["id"])
                        started = time.monotonic()
                        self._answer_jev(reply)
                        if reply["event"] == "jev_gate":
                            deadline += time.monotonic() - started
                        continue
                    break
            except (OSError, queue.Empty):
                self.cancel()
                raise EdgeBridgeError("profile_driver_outcome_unknown") from None
            if (not isinstance(reply, dict) or reply.get("id") != self._counter or self._cancelled.is_set()
                    or command != "close" and self._browser_closed):
                raise EdgeBridgeError("profile_driver_outcome_unknown")
            if reply.get("ok") is not True:
                code = reply.get("error")
                if not isinstance(code, str) or not re.fullmatch(r"(?:profile_driver|media_prepare|edge|browser_login)_[a-z_]{1,80}", code):
                    code = "profile_driver_command_failed"
                raise EdgeBridgeError(code)
            return reply.get("result")

    def set_jev_handler(self, handler):
        """handler(kind, run_id, payload) -> dict; kind is "jev_ask", "jev_gate" or "jev_type"."""
        self._jev_handler = handler

    def _answer_jev(self, event):
        reply = {"jev_reply": event.get("id"), "ok": False}
        handler = getattr(self, "_jev_handler", None)
        try:
            if callable(handler) and isinstance(event.get("id"), str):
                payload = event.get("info") if event["event"] == "jev_gate" else event.get("request")
                result = handler(event["event"], event.get("run_id"), payload)
                if isinstance(result, dict):
                    reply = {"jev_reply": event["id"], "ok": True, "result": result}
        except Exception:
            reply = {"jev_reply": event.get("id"), "ok": False}
        message = json.dumps(reply, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(message) > MAX_BYTES:
            message = json.dumps({"jev_reply": event.get("id"), "ok": False}).encode("utf-8") + b"\n"
        self._process.stdin.write(message)

    def request(self, command, params):
        if self._dedicated and command == "jev_run":
            seconds = params.get("max_seconds") if isinstance(params, dict) else None
            window = (seconds if type(seconds) is int and 5 <= seconds <= 900 else 900) + 610
            return self._rpc(command, params, timeout=window)
        if self._dedicated and command in DEDICATED_COMMANDS:
            return self._rpc(command, params)
        if self._dedicated or not command.startswith("media_prepare_"):
            raise EdgeBridgeError("profile_driver_command_rejected")
        return self._rpc(command, params)

    def local_setup(self, command, params):
        if command not in {"discover_targets", "select_target", "open_service"}:
            raise EdgeBridgeError("profile_driver_command_rejected")
        return self._rpc(command, params)

    def fixture_request(self, params):
        if not self._fixture:
            raise EdgeBridgeError("profile_driver_fixture_rejected")
        return self._rpc("fixture", params)

    def is_alive(self):
        return (not self._closed and not self._cancelled.is_set() and not self._browser_closed
                and self._process is not None and self._process.poll() is None
                and self._job.verify_identity(self._process.identity))

    def cancel(self):
        self._cancelled.set()
        if self._job is not None:
            self._verified_cleanup = self._job.close(timeout_seconds=5)
        elif self._process is None:
            # Job creation failed before any child could execute.
            self._verified_cleanup = True

    def close(self):
        if self._closed:
            return self._verified_cleanup
        with self._lock:
            if not self._cancelled.is_set() and self._process is not None and self._process.poll() is None:
                try:
                    self._rpc("close", {}, timeout=10)
                except EdgeBridgeError:
                    pass
            self.cancel()
            self._closed = True
            if self._process is not None:
                self._process.close()
            if self._verified_cleanup:
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            self.lease.finish_runtime()
                            break
                        except BrowserProfileError as error:
                            if str(error) != "browser_profile_runtime_still_alive" or time.monotonic() >= deadline:
                                raise
                            time.sleep(0.01)
                    for name in ("_reader", "_stderr"):
                        worker = getattr(self, name, None)
                        if worker is not None:
                            worker.join(timeout=1)
                    if _local_path(self._runtime_dir).parent != self._runtime_dir.parent:
                        raise ValueError()
                    shutil.rmtree(self._runtime_dir)
                except Exception as error:
                    if self._fixture:
                        self._diagnostic.extend((type(error).__name__ + ':' + str(error)).encode())
                    self._verified_cleanup = False
            return self._verified_cleanup
