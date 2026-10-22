"""Bounded, at-most-once execution of an explicitly packaged bridge helper.

There is no download, stock-helper fallback, browser launcher, shell, CDP client,
raw-output logging, or production non-Windows backend in this module. The
adapter owns browser-operation policy; this executor only accepts the public
metadata/strict CLI envelope. Private arguments travel through bounded stdin.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Mapping

from .agent_browser_supervisor import HelperManifest, verify_helper
from .edge_cdp import EdgeCdpEndpoint
from .windows_helper_job import ProcessIdentity, WindowsHelperJob

BASE_CAPABILITIES = frozenset(
    {
        "metadata-targets-v1",
        "existing-target-v1",
        "strict-refs-v1",
        "redact-editable-values-v1",
    }
)
DISCOVERY_CAPABILITY = "existing-target-discovery-v1"
MAX_STDIN_BYTES = 65536
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError


class AgentBrowserRuntimeError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class DaemonIdentity:
    process: ProcessIdentity
    port: int
    stamp: str


class OwnedHelperRuntime:
    def __init__(
        self,
        *,
        binary: Path,
        manifest: HelperManifest,
        required_capabilities: frozenset[str] | set[str] | tuple[str, ...],
        expected_daemon_stamp: str,
        session_id: str,
        state_directory: Path,
        endpoint: EdgeCdpEndpoint,
        attach_target: str | None = None,
        discover_existing: bool = False,
        inherited_environment: Mapping[str, str] | None = None,
        backend=None,
        daemon_observer=None,
    ):
        self.binary = verify_helper(binary, manifest)
        if not re.fullmatch(r"aoi-[a-f0-9]{32}", session_id):
            raise AgentBrowserRuntimeError("agent_browser_session_invalid")
        if type(discover_existing) is not bool:
            raise AgentBrowserRuntimeError("agent_browser_discovery_mode_invalid")
        if discover_existing and attach_target is not None:
            raise AgentBrowserRuntimeError("agent_browser_discovery_target_conflict")
        self._discover_existing = discover_existing
        required = frozenset(required_capabilities)
        if not required or any(
            not isinstance(cap, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", cap)
            for cap in required
        ):
            raise AgentBrowserRuntimeError("agent_browser_capabilities_invalid")
        self.required_capabilities = required | BASE_CAPABILITIES
        if self._discover_existing:
            self.required_capabilities |= {DISCOVERY_CAPABILITY}
        if not isinstance(expected_daemon_stamp, str) or not re.fullmatch(
            r"[A-Za-z0-9.+-]{1,128}", expected_daemon_stamp
        ):
            raise AgentBrowserRuntimeError("agent_browser_daemon_stamp_invalid")
        self.expected_daemon_stamp = expected_daemon_stamp
        self._daemon_observer = daemon_observer
        self._daemon: DaemonIdentity | None = None
        self._connect_attempted = False
        self.manifest, self.session_id, self.endpoint = manifest, session_id, endpoint
        self.state_directory = Path(state_directory)
        if (
            not self.state_directory.is_absolute()
            or str(self.state_directory).startswith(("\\\\", "//"))
            or self.state_directory.name != session_id
        ):
            raise AgentBrowserRuntimeError("agent_browser_state_path_invalid")
        self._check_path(self.state_directory)
        self._attach_target = (
            self._validate_target(attach_target) if attach_target is not None else None
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._backend = backend
        self._preflight_done = False
        self._browser_started = False
        self._closed = False
        self._cleanup_verified = False
        self._active_process = None
        self._active_threads: list[threading.Thread] = []
        self._pending_io = []
        self.config_path = self.state_directory / "agent-browser.json"
        inherited = (
            os.environ if inherited_environment is None else inherited_environment
        )
        self._environment = {
            key: value
            for key, value in inherited.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR"}
        }
        self._environment.update(
            {
                "TEMP": str(self.state_directory / "tmp"),
                "TMP": str(self.state_directory / "tmp"),
                "USERPROFILE": str(self.state_directory / "home"),
                "HOME": str(self.state_directory / "home"),
                "LOCALAPPDATA": str(self.state_directory / "home"),
                "APPDATA": str(self.state_directory / "home"),
                "AGENT_BROWSER_SOCKET_DIR": str(self.state_directory / "ipc"),
                "AGENT_BROWSER_NAMESPACE": "aoitalk-pc-bridge",
                "AGENT_BROWSER_NO_AUTO_DIALOG": "1",
                "AGENT_BROWSER_NO_WEBMCP": "1",
                "AGENT_BROWSER_AUTOSAVE_INTERVAL_MS": "0",
            }
        )
        try:
            # An existing directory is not a fresh owned session, even if empty.
            self.state_directory.mkdir(mode=0o700)
            for name in ("ipc", "tmp", "home"):
                (self.state_directory / name).mkdir(mode=0o700)
            self._write_config()
        except OSError:
            raise AgentBrowserRuntimeError(
                "agent_browser_state_create_failed"
            ) from None

    @staticmethod
    def _check_path(path: Path):
        for candidate in (path, *path.parents):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise AgentBrowserRuntimeError(
                    "agent_browser_state_path_invalid"
                ) from None
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise AgentBrowserRuntimeError("agent_browser_state_path_invalid")

    @staticmethod
    def _validate_target(value):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", value
        ):
            raise AgentBrowserRuntimeError("agent_browser_target_invalid")
        return value

    def _write_config(self):
        config = {
            "cdp": self.endpoint.url,
            "session": self.session_id,
            "namespace": "aoitalk-pc-bridge",
            "strictRefs": True,
            "noAutoDialog": True,
            "noWebmcp": True,
        }
        if self._discover_existing:
            config["discoverExisting"] = True
        elif self._attach_target is not None:
            config["attachTarget"] = self._attach_target
        contents = json.dumps(config, ensure_ascii=True, separators=(",", ":")) + "\n"
        temporary = self.state_directory / "agent-browser.new"
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(contents)
        temporary.replace(self.config_path)
        self._config_contents = contents

    def set_attach_target(self, target: str):
        with self._lock:
            self._ensure_open()
            target = self._validate_target(target)
            if self._attach_target is not None or (
                self._browser_started and not self._discover_existing
            ):
                if self._attach_target == target:
                    return
                raise AgentBrowserRuntimeError(
                    "agent_browser_initial_target_already_bound"
                )
            self._attach_target = target
            if self._discover_existing:
                # The daemon fingerprint covers bootstrap mode, not later tab
                # selection. Keep this config fixed for every owned CLI call.
                return
            try:
                self._write_config()
            except OSError:
                raise AgentBrowserRuntimeError(
                    "agent_browser_config_write_failed"
                ) from None

    @property
    def attach_target(self) -> str | None:
        return self._attach_target

    @property
    def discover_existing(self) -> bool:
        return self._discover_existing

    @property
    def epoch(self) -> str:
        # A stopped runtime cannot restart. A new runtime requires a fresh session.
        return self.session_id

    def _ensure_open(self):
        if self._closed or self._stop_event.is_set():
            raise AgentBrowserRuntimeError("agent_browser_runtime_closed")

    def _validate_files(self):
        verify_helper(self.binary, self.manifest)
        self._check_path(self.state_directory)
        for name in ("ipc", "tmp", "home", "agent-browser.json"):
            self._check_path(self.state_directory / name)
        try:
            info = self.config_path.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > 4096
                or self.config_path.read_text(encoding="utf-8") != self._config_contents
            ):
                raise AgentBrowserRuntimeError("agent_browser_config_changed")
        except OSError:
            raise AgentBrowserRuntimeError("agent_browser_config_unavailable") from None

    @staticmethod
    def _limits(timeout_seconds, max_output_bytes, stdin_bytes):
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120
        ):
            raise AgentBrowserRuntimeError("agent_browser_timeout_invalid")
        if (
            type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES
        ):
            raise AgentBrowserRuntimeError("agent_browser_output_limit_invalid")
        if stdin_bytes is not None and (
            not isinstance(stdin_bytes, bytes) or len(stdin_bytes) > MAX_STDIN_BYTES
        ):
            raise AgentBrowserRuntimeError("agent_browser_stdin_invalid")

    def preflight(self, *, timeout_seconds: float = 10, cancel_event=None) -> dict:
        with self._lock:
            self._ensure_open()
            if self._preflight_done:
                return dict(self._version)
            self._limits(timeout_seconds, 65536, None)
            version = self._execute(
                (str(self.binary), "--version", "--json"),
                None,
                timeout_seconds,
                cancel_event,
                65536,
            )
            capabilities = (
                version.get("capabilities") if isinstance(version, dict) else None
            )
            if (
                not isinstance(version, dict)
                or version.get("version") != self.manifest.version
                or type(version.get("bridgeProtocol")) is not int
                or version["bridgeProtocol"] != 1
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)
                or not self.required_capabilities.issubset(capabilities)
            ):
                self._terminate()
                raise AgentBrowserRuntimeError("agent_browser_bridge_contract_mismatch")
            self._version = version
            self._preflight_done = True
            return dict(version)

    def _command(self, args, stdin_bytes):
        if (
            not isinstance(args, (tuple, list))
            or not args
            or any(not isinstance(arg, str) or "\0" in arg for arg in args)
        ):
            raise AgentBrowserRuntimeError("agent_browser_command_rejected")
        args = tuple(args)
        metadata = args == ("cdp", "targets", self.endpoint.url)
        private = args == ("bridge-command", "--stdin")
        allowed = (
            metadata
            or private
            or args in {("connect", self.endpoint.url), ("tab", "list")}
        )
        selection = (
            args[:1] == ("tab",)
            and len(args) == 2
            and args[1] not in {"new", "close", "list"}
        )
        if selection:
            self._validate_target(args[1])
            allowed = True
        if args[:1] == ("snapshot",):
            remaining, seen = list(args[1:]), set()
            allowed = True
            while remaining:
                flag = remaining.pop(0)
                canonical = {
                    "-i": "--interactive",
                    "-c": "--compact",
                    "-d": "--depth",
                    "-s": "--selector",
                }.get(flag, flag)
                if canonical in seen:
                    allowed = False
                    break
                seen.add(canonical)
                if canonical in {
                    "--interactive",
                    "--compact",
                    "--redact-editable-values",
                }:
                    continue
                if canonical not in {"--depth", "--selector"} or not remaining:
                    allowed = False
                    break
                value = remaining.pop(0)
                if canonical == "--depth" and (
                    not value.isascii()
                    or not value.isdecimal()
                    or not 1 <= int(value) <= 100
                ):
                    allowed = False
                if canonical == "--selector" and (
                    not value or len(value) > 4096 or value.startswith("--")
                ):
                    allowed = False
            allowed = allowed and {
                "--interactive",
                "--redact-editable-values",
            }.issubset(seen)
        discovery_bootstrap = self._discover_existing and (
            selection or args in {("connect", self.endpoint.url), ("tab", "list")}
        )
        if private:
            if (
                "private-command-v1" not in self.required_capabilities
                or not stdin_bytes
            ):
                raise AgentBrowserRuntimeError(
                    "agent_browser_private_command_unavailable"
                )
            try:
                payload = json.loads(
                    stdin_bytes.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (ValueError, UnicodeError, RecursionError):
                raise AgentBrowserRuntimeError("agent_browser_stdin_invalid") from None
            if (
                not isinstance(payload, dict)
                or set(payload) != {"argv"}
                or not isinstance(payload["argv"], list)
                or not payload["argv"]
                or any(
                    not isinstance(arg, str) or "\0" in arg for arg in payload["argv"]
                )
            ):
                raise AgentBrowserRuntimeError("agent_browser_stdin_invalid")
            if payload["argv"] == ["tab", "list", "--local-labels"]:
                if "local-target-labels-v1" not in self.required_capabilities:
                    raise AgentBrowserRuntimeError("agent_browser_bridge_contract_mismatch")
                discovery_bootstrap = self._discover_existing
            if payload["argv"][0] == "crowdworks-inspect":
                if "crowdworks-inspection-v1" not in self.required_capabilities:
                    raise AgentBrowserRuntimeError("agent_browser_bridge_contract_mismatch")
                if payload["argv"] != ["crowdworks-inspect"]:
                    raise AgentBrowserRuntimeError("agent_browser_command_rejected")
        elif stdin_bytes is not None:
            raise AgentBrowserRuntimeError("agent_browser_stdin_invalid")
        if not allowed or (
            not metadata and self._attach_target is None and not discovery_bootstrap
        ):
            raise AgentBrowserRuntimeError("agent_browser_command_rejected")
        return (
            (str(self.binary), "--config", str(self.config_path), "--json", *args),
            metadata,
        )

    def _read_daemon_sidecars(self):
        directory = (
            self.state_directory / "ipc" / "namespaces" / "aoitalk-pc-bridge" / "run"
        )
        values = []
        for suffix in ("pid", "port", "version"):
            path = directory / f"{self.session_id}.{suffix}"
            self._check_path(path)
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 129:
                    raise ValueError
                with path.open("rb") as stream:
                    raw = stream.read(130)
                if not 1 <= len(raw) <= 129:
                    raise ValueError
                values.append(raw.decode("ascii").strip())
            except (OSError, ValueError, UnicodeError):
                raise AgentBrowserRuntimeError(
                    "agent_browser_daemon_state_invalid"
                ) from None
        pid_text, port_text, stamp = values
        if not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text) or not re.fullmatch(
            r"[1-9][0-9]{0,4}", port_text
        ):
            raise AgentBrowserRuntimeError("agent_browser_daemon_state_invalid")
        return int(pid_text), int(port_text), stamp

    def _guard_daemon(self, *, initial=False, outcome_unknown=False):
        try:
            observer = self._daemon_observer or self._read_daemon_sidecars
            pid, port, stamp = observer()
            if (
                type(pid) is not int
                or not 1 <= pid <= 0xFFFFFFFF
                or type(port) is not int
                or not 1 <= port <= 65535
                or stamp != self.expected_daemon_stamp
                or self._backend is None
            ):
                raise ValueError
            if initial:
                identity = self._backend.capture_identity(pid)
                if identity is None:
                    raise ValueError
                candidate = DaemonIdentity(identity, port, stamp)
            else:
                candidate = self._daemon
                if (
                    candidate is None
                    or candidate.process.pid != pid
                    or candidate.port != port
                    or candidate.stamp != stamp
                ):
                    raise ValueError
            if not self._backend.verify_identity(
                candidate.process
            ) or not self._backend.verify_port(candidate.process, port):
                raise ValueError
            # Re-read the files after process/port observation; never trust a
            # sidecar replacement that raced the first read.
            if observer() != (pid, port, stamp):
                raise ValueError
            if not self._backend.verify_identity(candidate.process):
                raise ValueError
            if initial:
                self._daemon = candidate
        except Exception:
            self._terminate()
            raise AgentBrowserRuntimeError(
                "agent_browser_daemon_ownership_lost", outcome_unknown=outcome_unknown
            ) from None

    def run(
        self,
        args,
        *,
        stdin_bytes: bytes | None = None,
        timeout_seconds: float = 10,
        cancel_event=None,
        max_output_bytes: int = 1024 * 1024,
    ) -> dict:
        with self._lock:
            self._ensure_open()
            self._limits(timeout_seconds, max_output_bytes, stdin_bytes)
            argv, metadata = self._command(args, stdin_bytes)
            deadline = time.monotonic() + timeout_seconds
            self.preflight(timeout_seconds=timeout_seconds, cancel_event=cancel_event)
            initial = not metadata and self._daemon is None
            if initial:
                if (
                    tuple(args) != ("connect", self.endpoint.url)
                    or self._connect_attempted
                ):
                    raise AgentBrowserRuntimeError(
                        "agent_browser_initial_connect_required"
                    )
                self._connect_attempted = True
            elif not metadata:
                self._guard_daemon()
            if not metadata:
                self._browser_started = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentBrowserRuntimeError("agent_browser_timeout")
            response = self._execute(
                argv, stdin_bytes, remaining, cancel_event, max_output_bytes
            )
            if not metadata and not self._closed:
                if initial and not response["success"]:
                    self._terminate()
                else:
                    self._guard_daemon(initial=initial, outcome_unknown=True)
            return response

    def _execute(
        self, argv, stdin_bytes, timeout_seconds, cancel_event, max_output_bytes
    ):
        self._ensure_open()
        if cancel_event is not None and cancel_event.is_set():
            raise AgentBrowserRuntimeError("agent_browser_cancelled")
        self._validate_files()
        try:
            if self._backend is None:
                self._backend = WindowsHelperJob()
            process = self._backend.spawn(
                argv, cwd=self.state_directory, environment=dict(self._environment)
            )
        except Exception:
            self._terminate()
            raise AgentBrowserRuntimeError(
                "agent_browser_spawn_failed", outcome_unknown=True
            ) from None
        self._active_process = process
        output = bytearray()
        overflow, io_error = threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        total = 0

        def read(stream, capture):
            nonlocal total
            try:
                register = getattr(process, "register_io_thread", None)
                if register is not None:
                    register()
                read_output = getattr(process, "read_output", None)
                while chunk := (
                    read_output(stream, 8192) if read_output else stream.read(8192)
                ):
                    with counter_lock:
                        total += len(chunk)
                        if total > max_output_bytes:
                            overflow.set()
                            return
                        if capture:
                            output.extend(chunk)
            except Exception:
                io_error.set()

        def write():
            try:
                register = getattr(process, "register_io_thread", None)
                if register is not None:
                    register()
                remaining = memoryview(stdin_bytes or b"")
                while remaining:
                    count = process.stdin.write(remaining[:8192])
                    if not count:
                        raise OSError
                    remaining = remaining[count:]
                process.stdin.close()
            except Exception:
                io_error.set()

        workers = [
            threading.Thread(target=read, args=(process.stdout, True), daemon=True),
            threading.Thread(target=read, args=(process.stderr, False), daemon=True),
            threading.Thread(target=write, daemon=True),
        ]
        self._active_threads = workers
        failure = None
        deadline = time.monotonic() + timeout_seconds
        try:
            for worker in workers:
                worker.start()
            while True:
                if self._stop_event.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    failure = "agent_browser_cancelled"
                elif overflow.is_set():
                    failure = "agent_browser_output_limit"
                elif io_error.is_set():
                    failure = "agent_browser_io_failed"
                elif time.monotonic() >= deadline:
                    failure = "agent_browser_timeout"
                if failure:
                    break
                exit_code = process.poll()
                if exit_code is not None and not any(
                    worker.is_alive() for worker in workers
                ):
                    break
                time.sleep(0.01)
            if failure:
                self._terminate()
                raise AgentBrowserRuntimeError(failure, outcome_unknown=True)
            try:
                result = json.loads(
                    output.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
                if not isinstance(result, dict):
                    raise ValueError
                if argv[-2:] == ("--version", "--json"):
                    if exit_code != 0:
                        raise ValueError
                else:
                    success = result.get("success")
                    if (
                        type(success) is not bool
                        or exit_code not in (0, 1)
                        or (exit_code == 1 and success)
                    ):
                        raise ValueError
                    if result.get("error") is not None and (
                        not isinstance(result["error"], str)
                        or len(result["error"]) > 4096
                    ):
                        raise ValueError
                    if result.get("code") is not None and (
                        not isinstance(result["code"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["code"])
                    ):
                        raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                self._terminate()
                raise AgentBrowserRuntimeError(
                    "agent_browser_json_invalid", outcome_unknown=True
                ) from None
            if result.get("code") == "outcome_unknown":
                self._terminate()
            return result
        except AgentBrowserRuntimeError:
            raise
        except Exception:
            self._terminate()
            raise AgentBrowserRuntimeError(
                "agent_browser_execution_failed", outcome_unknown=True
            ) from None
        finally:
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=0.25)
            # A broken kernel cancellation must not block the caller forever.
            # No retry is possible; a surviving pump owns its streams until exit.
            if any(worker.is_alive() for worker in workers):
                self._pending_io.append((process, workers))
                self._cleanup_verified = False
            else:
                try:
                    process.close()
                except Exception:
                    self._terminate()
                    self._cleanup_verified = False
                    raise AgentBrowserRuntimeError(
                        "agent_browser_cleanup_failed", outcome_unknown=True
                    ) from None
            self._active_process = None
            self._active_threads = []

    def _terminate(self):
        self._stop_event.set()
        self._closed = True
        if self._backend is not None:
            try:
                self._cleanup_verified = self._backend.close(timeout_seconds=2)
            except Exception:
                self._cleanup_verified = False
        else:
            self._cleanup_verified = True
        if self._active_process is not None:
            try:
                ids = tuple(
                    worker.native_id
                    for worker in self._active_threads
                    if worker.native_id is not None
                )
                self._active_process.cancel_io(ids)
            except Exception:
                pass

        pending = []
        for process, workers in self._pending_io:
            if any(worker.is_alive() for worker in workers):
                pending.append((process, workers))
            else:
                try:
                    process.close()
                except Exception:
                    pending.append((process, workers))
        self._pending_io = pending
        if pending:
            self._cleanup_verified = False

    def stop(self) -> bool:
        # Set before acquiring the operation lock, to cancel active pipe I/O.
        self._stop_event.set()
        with self._lock:
            self._terminate()
            return self._cleanup_verified

    close = stop

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def owned_processes(self) -> tuple[ProcessIdentity, ...]:
        return self._backend.records if self._backend is not None else ()

    def capture_owned_process(self, pid: int) -> ProcessIdentity | None:
        return (
            self._backend.capture_identity(pid) if self._backend is not None else None
        )

    def verify_owned_process(self, identity: ProcessIdentity) -> bool:
        return self._backend is not None and self._backend.verify_identity(identity)

    def verify_owned_port(self, identity: ProcessIdentity, port: int) -> bool:
        return self._backend is not None and self._backend.verify_port(identity, port)
