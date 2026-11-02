"""Long-lived, per-user bridge from Edge CDP to Playwright clients.

The broker owns the only CDP connection to the user's existing Edge instance.
AoiTalk processes discover it through an authenticated LOCAL named pipe and
connect to a Playwright WebSocket bound only to IPv4 loopback. A failed or
stale attempt is deliberately fail-closed: callers never cause an implicit
second CDP attach.

This module is the only production component permitted to read Edge's
DevToolsActivePort or establish the direct Edge CDP connection.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing.connection
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit


_APP_DIRECTORY = "AoiTalk/browser-broker"
_CONTROL_FILE = "control.json"
_AUTHKEY_FILE = "control.authkey"
_STATE_FILE = "connection-state.json"
_CONTROL_PROTOCOL_VERSION = 1
_STATE_PROTOCOL_VERSION = 1
_MAX_TIMEOUT_SEC = 60.0
# The caller's wait is deliberately short (up to 60s), but it must not bound the
# shared CDP attempt: Edge consent can be handled after an individual caller has
# returned `connecting`. Keep that one broker-owned attempt alive for 15 minutes
# so a later caller can join it instead of forcing another consent cycle.
_CDP_TIMEOUT_MS = 30_000
_BIND_TITLE = "AoiTalk dedicated Edge"
_PHASES = {"idle", "connecting", "ready", "failed"}
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9 ._/-]{1,200}$")
_PIPE_SUFFIX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DEVTOOLS_PORT = re.compile(r"^[1-9][0-9]{0,4}$")
_DEVTOOLS_PATH = re.compile(r"^/devtools/browser/[A-Za-z0-9_-]{1,128}$")
_SAFE_ERROR_CODE = re.compile(r"^edge_broker_[a-z0-9_]{1,64}$")
_SAFE_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


# Only fixed codes emitted by the broker may reach the optional diagnostics sink.
_DIAGNOSTIC_CODES = frozenset({
    "edge_broker_already_running", "edge_broker_browser_disconnected",
    "edge_broker_browser_bind_failed", "edge_broker_browser_version_failed",
    "edge_broker_cdp_connect_failed", "edge_broker_cdp_connect_timeout",
    "edge_broker_cdp_endpoint_failed", "edge_broker_cdp_endpoint_invalid",
    "edge_broker_cdp_endpoint_unavailable", "edge_broker_connect_failed",
    "edge_broker_edge_launch_failed", "edge_broker_edge_not_found",
    "edge_broker_endpoint_validation_failed", "edge_broker_failed",
    "edge_broker_listener_failed", "edge_broker_playwright_driver_start_failed",
    "edge_broker_profile_dir_invalid", "edge_broker_profile_in_use",
    "edge_broker_profile_migration_failed", "edge_broker_restart_limit",
    "edge_broker_state_invalid", "edge_broker_storage_unavailable",
    "edge_broker_unavailable", "edge_broker_version_unavailable",
    "edge_broker_windows_required",
})
_DIAGNOSTIC_LIMIT = 16_384


def _enable_console_ctrl_c() -> bool:
    """Undo the new process group's inherited Ctrl+C ignore flag in the broker."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
        return bool(kernel.SetConsoleCtrlHandler(None, False))
    except Exception:
        return False


def _own_console(message: str, *, startup: bool = False) -> None:
    """Write to an existing Windows console, independently of redirected stdio."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleWindow.argtypes = []
        kernel.GetConsoleWindow.restype = wintypes.HWND
        kernel.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
        kernel.SetConsoleTitleW.restype = wintypes.BOOL
        if not kernel.GetConsoleWindow():
            return
        if startup:
            kernel.SetConsoleTitleW("AoiTalk - local browser CDP broker")
        with open("CONOUT$", "w", encoding="utf-8") as console:
            console.write(message + "\n")
    except Exception:
        pass


class _BrokerDiagnostics:
    """Best-effort, value-free transition messages with bounded disk/console output."""

    def __init__(self, directory: Path, console: Callable[..., None] | None = None) -> None:
        self.path = directory / "broker-diagnostics.log"
        self.console = console or _own_console
        self._last: tuple[str, str | None] | None = None

    def start(self) -> None:
        self._emit(f"AoiTalk local browser CDP broker | connection: dedicated Edge CDP / local named pipe and loopback WebSocket | log: {self.path}", startup=True)

    def transition(self, phase: str, code: str | None = None) -> None:
        if phase not in _PHASES:
            return
        safe_code = code if code in _DIAGNOSTIC_CODES else ("edge_broker_failed" if code else None)
        current = (phase, safe_code)
        if current == self._last:
            return
        self._last = current
        self._emit(f"state: {phase}" + (f" | error: {safe_code}" if safe_code else ""))

    def _emit(self, message: str, *, startup: bool = False) -> None:
        try:
            self.console(message, startup=startup)
        except Exception:
            pass
        try:
            data = (message + "\n").encode("utf-8")
            if len(data) > _DIAGNOSTIC_LIMIT:
                return
            if self.path.exists() and self.path.stat().st_size + len(data) > _DIAGNOSTIC_LIMIT:
                os.replace(self.path, self.path.with_suffix(".log.1"))
            with self.path.open("ab") as stream:
                stream.write(data)
        except Exception:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EdgeBrokerError(RuntimeError):
    """An error with a fixed, non-secret public code."""

    def __init__(self, code: str, *, cdp_connected_success: bool | None = None) -> None:
        if not re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code):
            code = "edge_broker_unavailable"
        super().__init__(code)
        self.code = code
        self.cdp_connected_success = cdp_connected_success


def _raise(code: str, *, cdp_connected_success: bool | None = None) -> None:
    raise EdgeBrokerError(code, cdp_connected_success=cdp_connected_success) from None


@dataclass(frozen=True)
class BrokerPaths:
    directory: Path
    control_file: Path
    authkey_file: Path
    state_file: Path


def _paths(local_app_data: str | os.PathLike[str] | None = None) -> BrokerPaths:
    root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not root:
        _raise("edge_broker_storage_unavailable")
    base = Path(root)
    if not base.is_absolute():
        _raise("edge_broker_storage_unavailable")
    directory = base / _APP_DIRECTORY
    return BrokerPaths(
        directory=directory,
        control_file=directory / _CONTROL_FILE,
        authkey_file=directory / _AUTHKEY_FILE,
        state_file=directory / _STATE_FILE,
    )


def broker_state_dir() -> Path:
    """Return the per-user broker directory without creating it."""
    return _paths().directory


def connection_state_path() -> Path:
    """Return the durable broker diagnostics path."""
    return _paths().state_file


def playwright_registry_dir() -> Path:
    """Return the protected Playwright server-registry subdirectory."""
    return broker_state_dir() / "playwright-registry"


def _user_pipe_suffix() -> str:
    # The pipe name is stable for this Windows user but contains no username.
    identity = "\\".join(
        (
            os.environ.get("USERDOMAIN", ""),
            os.environ.get("USERNAME", ""),
            os.environ.get("LOCALAPPDATA", ""),
        )
    )
    return hashlib.sha256(identity.casefold().encode("utf-8", "replace")).hexdigest()[:24]


def _pipe_address() -> str:
    return rf"\\.\pipe\LOCAL\AoiTalk.BrowserBroker.{_user_pipe_suffix()}"


def _validate_pipe_address(value: Any) -> str:
    """Accept only a local Windows named pipe; reject URLs and remote hosts."""
    if not isinstance(value, str) or len(value) > 240:
        _raise("edge_broker_invalid_endpoint")
    prefix = "\\\\.\\pipe\\LOCAL\\"
    if not value.casefold().startswith(prefix.casefold()):
        _raise("edge_broker_invalid_endpoint")
    suffix = value[len(prefix) :]
    if not _PIPE_SUFFIX.fullmatch(suffix):
        _raise("edge_broker_invalid_endpoint")
    if value.casefold() != _pipe_address().casefold():
        _raise("edge_broker_invalid_endpoint")
    return value


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
    except OSError:
        _raise("edge_broker_unavailable")
    if len(value) > limit:
        _raise("edge_broker_invalid_control")
    return value


def _read_control_config(local_app_data: str | os.PathLike[str] | None = None) -> tuple[str, bytes]:
    paths = _paths(local_app_data)
    raw = _read_bounded(paths.control_file, 2_048)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise("edge_broker_invalid_control")
    if not isinstance(payload, dict) or payload.get("version") != _CONTROL_PROTOCOL_VERSION:
        _raise("edge_broker_invalid_control")
    address = _validate_pipe_address(payload.get("pipe"))
    # The endpoint file cannot redirect an AoiTalk client to another local pipe.
    if address != _pipe_address():
        _raise("edge_broker_invalid_endpoint")
    key = _read_bounded(paths.authkey_file, 64)
    if len(key) != 32:
        _raise("edge_broker_invalid_control")
    return address, key


def _write_bytes_exclusive(path: Path, value: bytes) -> bytes:
    try:
        with path.open("xb") as stream:
            stream.write(value)
        return value
    except FileExistsError:
        return _read_bounded(path, 64)
    except OSError:
        _raise("edge_broker_storage_unavailable")


def _load_or_create_authkey(paths: BrokerPaths) -> bytes:
    try:
        key = _read_bounded(paths.authkey_file, 64)
    except EdgeBrokerError as exc:
        if exc.code != "edge_broker_unavailable":
            raise
        key = _write_bytes_exclusive(paths.authkey_file, secrets.token_bytes(32))
    if len(key) != 32:
        _raise("edge_broker_invalid_control")
    return key


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        _raise("edge_broker_storage_unavailable")


class BrokerState:
    """Persist broker phase and fixed diagnostics, scoped to the current user."""

    def __init__(self, path: Path, diagnostics: _BrokerDiagnostics | None = None) -> None:
        self.path = path
        self.diagnostics = diagnostics

    def read_diagnostics(self) -> dict[str, Any]:
        """Read bounded, value-free connection diagnostics from the state file."""
        if not self.path.exists():
            return {
                "phase": None,
                "error_code": None,
                "error_at_utc": None,
                "cdp_connected_success": None,
            }
        raw = _read_bounded(self.path, 1_024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _STATE_PROTOCOL_VERSION
            or payload.get("phase") not in _PHASES
        ):
            return {
                "phase": None,
                "error_code": None,
                "error_at_utc": None,
                "cdp_connected_success": None,
            }
        error_code = payload.get("error_code")
        if not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code):
            error_code = None
        error_at_utc = payload.get("error_at_utc")
        if not isinstance(error_at_utc, str) or not _SAFE_UTC_TIMESTAMP.fullmatch(error_at_utc):
            error_at_utc = None
        cdp_success = payload.get("cdp_connected_success")
        if type(cdp_success) is not bool:
            cdp_success = None
        return {
            "phase": str(payload["phase"]),
            "error_code": error_code,
            "error_at_utc": error_at_utc,
            "cdp_connected_success": cdp_success,
        }

    def _read_phase(self) -> str | None:
        return self.read_diagnostics()["phase"]

    def write(
        self,
        phase: str,
        *,
        error_code: str | None = None,
        error_at_utc: str | None = None,
        cdp_connected_success: bool | None = False,
    ) -> None:
        if phase not in _PHASES:
            _raise("edge_broker_state_invalid")
        if error_code is not None and not _SAFE_ERROR_CODE.fullmatch(error_code):
            _raise("edge_broker_state_invalid")
        if error_at_utc is not None and not _SAFE_UTC_TIMESTAMP.fullmatch(error_at_utc):
            _raise("edge_broker_state_invalid")
        if cdp_connected_success is not None and type(cdp_connected_success) is not bool:
            _raise("edge_broker_state_invalid")
        _atomic_json_write(
            self.path,
            {
                "version": _STATE_PROTOCOL_VERSION,
                "phase": phase,
                "updated_at_utc": _utc_now(),
                "error_code": error_code,
                "error_at_utc": error_at_utc,
                "cdp_connected_success": cdp_connected_success,
            },
        )
        if self.diagnostics is not None:
            try:
                self.diagnostics.transition(phase, error_code)
            except Exception:
                pass

    def prepare_startup(self) -> str:
        """Begin a fresh dedicated-browser attempt and clear stale diagnostics."""
        self.write("idle", cdp_connected_success=False)
        return "idle"


@dataclass
class RuntimeHandle:
    driver: Any
    browser: Any
    endpoint: str
    browser_version: str


def _safe_browser_version(value: Any) -> str:
    text = str(value or "").strip()[:200]
    return text if _SAFE_VERSION.fullmatch(text) else ""


def _is_playwright_timeout(exc: Exception) -> bool:
    """Recognize a CDP connect timeout without examining its private message."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception:
        return False
    return isinstance(exc, PlaywrightTimeoutError)


def _validate_loopback_ws_endpoint(value: Any) -> str:
    """Accept only the ephemeral loopback WebSocket emitted by Browser.bind."""
    if not isinstance(value, str) or len(value) > 512:
        _raise("edge_broker_invalid_endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _raise("edge_broker_invalid_endpoint")
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Fa-f0-9]{32}", parsed.path)
    ):
        _raise("edge_broker_invalid_endpoint")
    return value


def _read_devtools_endpoint(
    active_port_path: str | os.PathLike[str],
) -> str:
    """Read the dedicated profile's DevToolsActivePort and force loopback."""
    active_port = Path(active_port_path)
    try:
        with active_port.open("rb") as stream:
            raw = stream.read(513)
    except OSError:
        _raise("edge_broker_cdp_endpoint_unavailable")
    if not 1 <= len(raw) <= 512:
        _raise("edge_broker_cdp_endpoint_invalid")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _raise("edge_broker_cdp_endpoint_invalid")
    if (
        len(lines) != 2
        or not _DEVTOOLS_PORT.fullmatch(lines[0])
        or not _DEVTOOLS_PATH.fullmatch(lines[1])
        or int(lines[0]) > 65_535
    ):
        _raise("edge_broker_cdp_endpoint_invalid")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"


def _loopback_tcp_probe(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _can_open_lockfile_exclusively(path: Path) -> bool:
    """Return whether the profile lockfile can be opened with no sharing."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            return False
        close_handle(handle)
        return True
    except Exception:
        return False


def _migrate_legacy_profile(
    local_app_data: Path,
    profile_dir: Path,
    *,
    exists: Callable[[Path], bool] | None = None,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], Any] | None = None,
    mkdir: Callable[[Path], Any] | None = None,
    can_open_lockfile: Callable[[Path], bool] | None = None,
    endpoint_reader: Callable[[Path], str] | None = None,
    tcp_probe: Callable[[int], bool] | None = None,
) -> bool:
    """Move the previous dedicated profile once, only after proving it is idle.

    Operations are injectable so migration behavior can be verified without an
    Edge process or touching the user's profile.
    """
    file_exists = exists or (lambda path: path.exists())
    replace_path = replace or os.replace
    make_directory = mkdir or (lambda path: path.mkdir(parents=True, exist_ok=True))
    lock_probe = can_open_lockfile or _can_open_lockfile_exclusively
    read_endpoint = endpoint_reader or (
        lambda path: _read_devtools_endpoint(active_port_path=path)
    )
    port_probe = tcp_probe or _loopback_tcp_probe

    old_profile = local_app_data / ("Home" + "MCPGateway") / "edge-profile"
    if file_exists(profile_dir) or not file_exists(old_profile):
        return False

    lockfile = old_profile / "lockfile"
    try:
        lock_is_free = file_exists(lockfile) and lock_probe(lockfile)
    except Exception:
        lock_is_free = False
    port_is_live = False
    if not lock_is_free:
        try:
            port = urlsplit(read_endpoint(old_profile / "DevToolsActivePort")).port
            port_is_live = port is not None and port_probe(port)
        except Exception:
            port_is_live = False
    if port_is_live:
        _raise("edge_broker_profile_in_use")

    try:
        make_directory(profile_dir.parent)
        replace_path(old_profile, profile_dir)
    except OSError:
        _raise("edge_broker_profile_migration_failed")
    return True


class DedicatedEdge:
    """Starts and reuses the broker-owned Edge profile without touching daily Edge."""

    def __init__(
        self,
        local_app_data: str | os.PathLike[str] | None = None,
        *,
        profile_dir: str | os.PathLike[str] | None = None,
        popen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        tcp_probe: Callable[[int], bool] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 0.25,
        profile_migration: Callable[[Path, Path], Any] | None = None,
        process_iter: Callable[[], Any] | None = None,
    ) -> None:
        self._process_iter = process_iter
        root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
        if not root or not Path(root).is_absolute():
            _raise("edge_broker_profile_dir_invalid")
        self.local_app_data = Path(root)
        self.profile_dir = (
            Path(profile_dir)
            if profile_dir is not None
            else self.local_app_data / "AoiTalk" / "browser-profile"
        )
        self.profile_migration = profile_migration or (
            _migrate_legacy_profile if profile_dir is None else None
        )
        self._validate_profile_dir()
        self.popen = popen or subprocess.Popen
        self.clock = clock or time.monotonic
        self.tcp_probe = tcp_probe or _loopback_tcp_probe
        self.sleep = sleep or asyncio.sleep
        self.timeout_sec = max(0.0, float(timeout_sec))
        self.poll_interval_sec = max(0.01, float(poll_interval_sec))

    @property
    def active_port_path(self) -> Path:
        return self.profile_dir / "DevToolsActivePort"

    def _validate_profile_dir(self) -> None:
        if not self.profile_dir.is_absolute():
            _raise("edge_broker_profile_dir_invalid")
        default_profile = self.local_app_data / "Microsoft" / "Edge" / "User Data"
        try:
            profile = os.path.normcase(os.path.normpath(str(self.profile_dir.resolve(strict=False))))
            default = os.path.normcase(os.path.normpath(str(default_profile.resolve(strict=False))))
            common = os.path.normcase(os.path.commonpath((profile, default)))
        except (OSError, RuntimeError, ValueError):
            _raise("edge_broker_profile_dir_invalid")
        if common == profile or common == default:
            _raise("edge_broker_profile_dir_invalid")

    def _edge_executable(self) -> Path:
        candidates: list[Path] = []
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            root = os.environ.get(variable)
            if root and Path(root).is_absolute():
                candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        _raise("edge_broker_edge_not_found")

    def _available_port(self) -> int | None:
        try:
            endpoint = _read_devtools_endpoint(active_port_path=self.active_port_path)
            port = urlsplit(endpoint).port
        except (EdgeBrokerError, ValueError):
            return None
        if port is not None:
            try:
                if self.tcp_probe(port):
                    return port
            except Exception:
                pass
        return None

    def terminate(self, *, grace_sec: float = 10.0) -> int:
        """Stop the Edge processes that run this dedicated profile; never any other Edge.

        An Edge that accepts TCP but never completes the CDP handshake is wedged.
        Closing it lets the next connect attempt launch a fresh one on the same
        profile (logins are persisted in the profile).  Returns how many it stopped.
        """
        self._validate_profile_dir()
        process_iter = self._process_iter
        if process_iter is None:
            import psutil

            def process_iter():
                return psutil.process_iter(["name", "cmdline"])

        wanted = os.path.normcase(os.path.normpath(str(self.profile_dir)))
        matched = []
        for process in process_iter():
            try:
                info = process.info
                if (info.get("name") or "").lower() != "msedge.exe":
                    continue
                for argument in info.get("cmdline") or ():
                    if argument.startswith("--user-data-dir="):
                        value = argument.split("=", 1)[1].strip('"')
                        if os.path.normcase(os.path.normpath(value)) == wanted:
                            matched.append(process)
                        break
            except Exception:
                continue
        # The browser (parent) process first, so Edge closes its children itself.
        for process in matched:
            try:
                if not any(a.startswith("--type=") for a in (process.info.get("cmdline") or ())):
                    process.terminate()
            except Exception:
                pass
        deadline = time.monotonic() + grace_sec
        while time.monotonic() < deadline and any(_still_running(p) for p in matched):
            time.sleep(0.2)
        for process in matched:
            if _still_running(process):
                try:
                    process.kill()
                except Exception:
                    pass
        try:
            self.active_port_path.unlink(missing_ok=True)
        except OSError:
            pass
        return len(matched)

    async def ensure_running(self) -> None:
        """Reuse a live loopback endpoint or launch Edge once and wait up to 30s."""
        self._validate_profile_dir()
        if self.profile_migration is not None:
            self.profile_migration(self.local_app_data, self.profile_dir)
        if self._available_port() is not None:
            return

        try:
            self.active_port_path.unlink(missing_ok=True)
        except OSError:
            _raise("edge_broker_edge_launch_failed")

        executable = self._edge_executable()
        args = [
            str(executable),
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            # The dedicated profile must stay a clean QA browser: Windows auto sign-in would
            # otherwise sync the daily profile's extensions, passwords and autofill into it.
            "--disable-sync",
            "--disable-extensions",
            "about:blank",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            self.popen(args, shell=False, creationflags=creationflags)
        except Exception:
            _raise("edge_broker_edge_launch_failed")

        deadline = self.clock() + self.timeout_sec
        while True:
            if self._available_port() is not None:
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                _raise("edge_broker_edge_launch_failed")
            await self.sleep(min(self.poll_interval_sec, remaining))


def _still_running(process: Any) -> bool:
    try:
        return bool(process.is_running())
    except Exception:
        return False


def _dedicated_edge_opener(
    dedicated_edge: DedicatedEdge,
    *,
    playwright_factory: Callable[[], Any] | None = None,
) -> Callable[[], Awaitable[RuntimeHandle]]:
    async def open_runtime() -> RuntimeHandle:
        try:
            return await _open_edge_runtime(
                playwright_factory=playwright_factory,
                endpoint_reader=lambda: _read_devtools_endpoint(
                    active_port_path=dedicated_edge.active_port_path
                ),
                cdp_timeout_ms=_CDP_TIMEOUT_MS,
                prelaunch=dedicated_edge.ensure_running,
            )
        except EdgeBrokerError as exc:
            if exc.code == "edge_broker_cdp_connect_timeout":
                # Edge answers on TCP but never finishes the CDP handshake: it is wedged and
                # retrying against it only repeats the timeout.  Close it; the next attempt
                # launches a fresh one on the same profile.
                try:
                    await asyncio.to_thread(dedicated_edge.terminate)
                except Exception:
                    pass
            raise

    return open_runtime


async def _open_edge_runtime(
    *,
    playwright_factory: Callable[[], Any] | None = None,
    endpoint_reader: Callable[[], str] | None = None,
    cdp_timeout_ms: int = _CDP_TIMEOUT_MS,
    prelaunch: Callable[[], Awaitable[None]] | None = None,
) -> RuntimeHandle:
    """Make the broker's one CDP connection and bind a local Playwright WebSocket."""
    if playwright_factory is None:
        from playwright.async_api import async_playwright

        playwright_factory = async_playwright
    if endpoint_reader is None:
        _raise("edge_broker_cdp_endpoint_unavailable")
    driver = None
    stage = "cdp_endpoint_discovery"
    cdp_connected_success = False
    try:
        if prelaunch is not None:
            await prelaunch()
        cdp_endpoint = endpoint_reader()
        registry_dir = playwright_registry_dir()
        try:
            registry_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _raise("edge_broker_storage_unavailable")
        stage = "playwright_driver_start"
        playwright = playwright_factory()
        env_was_set = "PWTEST_SERVER_REGISTRY" in os.environ
        previous_registry = os.environ.get("PWTEST_SERVER_REGISTRY")
        os.environ["PWTEST_SERVER_REGISTRY"] = str(registry_dir)
        try:
            # Playwright copies the environment when its driver subprocess starts.
            # Restore this process immediately afterwards; the broker itself is
            # a separate process and no parent-process environment is modified.
            stage = "playwright_driver_start"
            driver = await playwright.start()
        finally:
            if env_was_set:
                os.environ["PWTEST_SERVER_REGISTRY"] = previous_registry or ""
            else:
                os.environ.pop("PWTEST_SERVER_REGISTRY", None)
        stage = "cdp_connect"
        browser = await driver.chromium.connect_over_cdp(
            cdp_endpoint,
            timeout=cdp_timeout_ms,
            no_defaults=True,
        )
        cdp_connected_success = True
        stage = "browser_version"
        session = await browser.new_browser_cdp_session()
        try:
            version_info = await session.send("Browser.getVersion")
        finally:
            await session.detach()
        version = _safe_browser_version(version_info.get("product"))
        if not version:
            _raise("edge_broker_version_unavailable", cdp_connected_success=True)
        # Pin this Playwright server to IPv4 loopback. Port 0 requests an
        # ephemeral port; the endpoint is returned only over authenticated IPC.
        stage = "browser_bind"
        bound = await browser.bind(_BIND_TITLE, host="127.0.0.1", port=0)
        endpoint = bound.get("endpoint") if isinstance(bound, dict) else getattr(bound, "endpoint", None)
        stage = "endpoint_validation"
        try:
            endpoint = _validate_loopback_ws_endpoint(endpoint)
        except EdgeBrokerError:
            _raise("edge_broker_endpoint_validation_failed", cdp_connected_success=True)
        return RuntimeHandle(driver, browser, endpoint, version)
    except asyncio.CancelledError:
        if driver is not None:
            try:
                await driver.stop()
            except Exception:
                pass
        raise
    except EdgeBrokerError:
        if driver is not None:
            try:
                await driver.stop()
            except Exception:
                pass
        raise
    except Exception as exc:
        # Do not propagate Playwright errors, which can contain local endpoints.
        if driver is not None:
            try:
                await driver.stop()
            except Exception:
                pass
        stage_codes = {
            "cdp_endpoint_discovery": "edge_broker_cdp_endpoint_failed",
            "playwright_driver_start": "edge_broker_playwright_driver_start_failed",
            "cdp_connect": "edge_broker_cdp_connect_failed",
            "browser_version": "edge_broker_browser_version_failed",
            "browser_bind": "edge_broker_browser_bind_failed",
            "endpoint_validation": "edge_broker_endpoint_validation_failed",
        }
        if stage == "cdp_connect" and _is_playwright_timeout(exc):
            _raise("edge_broker_cdp_connect_timeout", cdp_connected_success=cdp_connected_success)
        _raise(stage_codes.get(stage, "edge_broker_connect_failed"), cdp_connected_success=cdp_connected_success)


class EdgeBrokerCore:
    """Single-flight state machine, injectable for CPU-only tests."""

    def __init__(
        self,
        state: BrokerState,
        opener: Callable[[], Awaitable[RuntimeHandle]] | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.state = state
        self.phase = state.prepare_startup()
        self.opener = opener or _open_edge_runtime
        self.runtime: RuntimeHandle | None = None
        self.error_code = ""
        self.error_at_utc = None
        self.cdp_connected_success = False
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task[None] | None = None
        self._clock = clock or time.monotonic
        self._restart_attempts: list[float] = []
        self._consecutive_failures = 0
        self._next_retry_at: float | None = None
        self._last_failure_code = ""

    def _connected(self) -> bool:
        """The CDP connection is live AND the endpoint handed to clients still accepts.

        Playwright keeps reporting ``is_connected()`` after its driver process dies (a
        restart script that kills node.exe does that), while the bound client endpoint
        is already gone.  Probing the endpoint's loopback port catches that.
        """
        runtime = self.runtime
        if runtime is None:
            return False
        try:
            if not runtime.browser.is_connected():
                return False
            port = urlsplit(runtime.endpoint).port
        except Exception:
            return False
        if port is None:
            return False
        try:
            return bool(_loopback_tcp_probe(port))
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        """Return value-free broker state without triggering or changing a connect."""
        cdp_connected = self.phase == "ready" and self._connected()
        return {
            "phase": self.phase,
            "mode": "dedicated",
            "error_code": self.error_code or None,
            "error_at_utc": self.error_at_utc,
            "cdp_connected_success": self.cdp_connected_success,
            "cdp_connected": cdp_connected,
            "broker_pid": os.getpid(),
            "status_ipc_supported": True,
        }

    async def ensure_browser(self, wait_sec: float) -> dict[str, str]:
        if type(wait_sec) not in (int, float) or not math.isfinite(wait_sec):
            return {"phase": "failed", "error": "edge_broker_invalid_timeout"}
        wait_sec = max(0.0, min(float(wait_sec), _MAX_TIMEOUT_SEC))
        async with self._lock:
            reconnect = False
            if self.phase == "ready":
                if self.runtime is not None and self._connected():
                    return {
                        "phase": "ready",
                        "endpoint": self.runtime.endpoint,
                        "browser_version": self.runtime.browser_version,
                    }
                await self._stop_runtime_driver()
                self.runtime = None
                self.phase = "idle"
                self.error_code = "edge_broker_browser_disconnected"
                self.error_at_utc = _utc_now()
                self.cdp_connected_success = True
                try:
                    self.state.write("idle", cdp_connected_success=True)
                except EdgeBrokerError:
                    self._mark_failed("edge_broker_storage_unavailable", cdp_connected_success=True)
                    return {"phase": "failed", "error": self.error_code}
                reconnect = True
            if self.phase == "failed":
                if self.runtime is not None:
                    await self._stop_runtime_driver()
                    self.runtime = None
                reconnect = True
            if reconnect:
                now = self._clock()
                self._restart_attempts = [
                    attempted_at for attempted_at in self._restart_attempts
                    if now - attempted_at < 600.0
                ]
                if self._next_retry_at is not None and now < self._next_retry_at:
                    return {"phase": "failed", "error": self.error_code or "edge_broker_failed"}
                if len(self._restart_attempts) >= 5:
                    self._mark_restart_limit()
                    return {"phase": "failed", "error": "edge_broker_restart_limit"}
                self._restart_attempts.append(now)
                if self.runtime is not None:
                    await self._stop_runtime_driver()
                    self.runtime = None
                try:
                    self.state.write("idle", cdp_connected_success=self.cdp_connected_success)
                except EdgeBrokerError:
                    self._mark_failed("edge_broker_storage_unavailable", cdp_connected_success=self.cdp_connected_success)
                    return {"phase": "failed", "error": self.error_code}
                self.phase = "idle"
                if self.error_code == "edge_broker_restart_limit":
                    self.error_code = self._last_failure_code or "edge_broker_failed"
            if self.phase == "idle":
                try:
                    # Persist before starting a connect task; a crash is stale/blocked.
                    self.state.write("connecting", cdp_connected_success=False)
                except EdgeBrokerError:
                    self._mark_failed("edge_broker_storage_unavailable", cdp_connected_success=False)
                    return {"phase": "failed", "error": self.error_code}
                self.phase = "connecting"
                self._connect_task = asyncio.create_task(self._connect_once())
            task = self._connect_task

        if task is None:
            return {"phase": "failed", "error": "edge_broker_state_invalid"}
        if wait_sec <= 0:
            return {"phase": "connecting"}
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_sec)
        except asyncio.TimeoutError:
            return {"phase": "connecting"}
        async with self._lock:
            if self.phase == "ready" and self.runtime is not None:
                return {
                    "phase": "ready",
                    "endpoint": self.runtime.endpoint,
                    "browser_version": self.runtime.browser_version,
                }
            return {"phase": "failed", "error": self.error_code or "edge_broker_failed"}

    async def _connect_once(self) -> None:
        runtime: RuntimeHandle | None = None
        committed = False
        try:
            runtime = await self.opener()
            endpoint = _validate_loopback_ws_endpoint(runtime.endpoint)
            version = _safe_browser_version(runtime.browser_version)
            if not version:
                _raise("edge_broker_version_unavailable")
            self.state.write("ready", cdp_connected_success=True)
            async with self._lock:
                self.runtime = RuntimeHandle(runtime.driver, runtime.browser, endpoint, version)
                self.phase = "ready"
                self.error_code = ""
                self.error_at_utc = None
                self.cdp_connected_success = True
                self._restart_attempts.clear()
                self._consecutive_failures = 0
                self._next_retry_at = None
                self._last_failure_code = ""
                committed = True
        except EdgeBrokerError as exc:
            cdp_success = exc.cdp_connected_success
            if cdp_success is None:
                cdp_success = runtime is not None
            self._mark_failed(exc.code, cdp_connected_success=cdp_success)
        except Exception:
            self._mark_failed("edge_broker_connect_failed", cdp_connected_success=runtime is not None)
        finally:
            if runtime is not None and not committed:
                # The attempt cannot be reused. Stop only Playwright's local
                # transport; never close the borrowed browser or its Edge owner.
                try:
                    await runtime.driver.stop()
                except Exception:
                    pass

    def _mark_failed(self, code: str, *, cdp_connected_success: bool | None = None) -> None:
        self.phase = "failed"
        self.error_code = code if re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code) else "edge_broker_failed"
        self.error_at_utc = _utc_now()
        if cdp_connected_success is not None:
            self.cdp_connected_success = cdp_connected_success
        self.cdp_connected_success = bool(self.cdp_connected_success)
        self._last_failure_code = self.error_code
        self._consecutive_failures += 1
        delays = (5.0, 15.0, 45.0, 120.0)
        delay = delays[min(self._consecutive_failures - 1, len(delays) - 1)]
        self._next_retry_at = self._clock() + delay
        try:
            self.state.write(
                "failed",
                error_code=self.error_code,
                error_at_utc=self.error_at_utc,
                cdp_connected_success=self.cdp_connected_success,
            )
        except EdgeBrokerError:
            # The prior connecting marker remains, so a new process fails closed.
            pass

    def _mark_restart_limit(self) -> None:
        self.phase = "failed"
        self._last_failure_code = self._last_failure_code or self.error_code or "edge_broker_failed"
        self.error_code = "edge_broker_restart_limit"
        self.error_at_utc = _utc_now()
        try:
            self.state.write(
                "failed",
                error_code=self.error_code,
                error_at_utc=self.error_at_utc,
                cdp_connected_success=self.cdp_connected_success,
            )
        except EdgeBrokerError:
            pass

    async def _stop_runtime_driver(self) -> None:
        if self.runtime is not None:
            try:
                await self.runtime.driver.stop()
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Stop only the broker's Playwright driver transport, never Edge."""
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        if self.runtime is not None:
            try:
                await self.runtime.driver.stop()
            except Exception:
                pass


def _request_control_sync(address: str, authkey: bytes, wait_sec: float) -> Any:
    connection = None
    try:
        connection = multiprocessing.connection.Client(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        connection.send({"op": "ensure_browser", "wait_sec": wait_sec})
        if not connection.poll(wait_sec + 1.0):
            _raise("edge_broker_timeout")
        return connection.recv()
    finally:
        if connection is not None:
            connection.close()


def _request_status_sync(address: str, authkey: bytes, wait_sec: float) -> Any:
    """Request diagnostics only; this operation never calls ensure_browser."""
    connection = None
    try:
        connection = multiprocessing.connection.Client(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        connection.send({"op": "status"})
        if not connection.poll(wait_sec + 1.0):
            _raise("edge_broker_timeout")
        return connection.recv()
    finally:
        if connection is not None:
            connection.close()


async def get_broker_status(timeout_sec: float = 3.0) -> dict[str, Any]:
    """Read authenticated broker diagnostics without triggering an Edge attach."""
    if (
        type(timeout_sec) not in (int, float)
        or not math.isfinite(timeout_sec)
        or not 1 <= timeout_sec <= _MAX_TIMEOUT_SEC
    ):
        _raise("edge_broker_invalid_timeout")
    try:
        address, authkey = await asyncio.to_thread(_read_control_config)
        response = await asyncio.wait_for(
            asyncio.to_thread(_request_status_sync, address, authkey, float(timeout_sec)),
            timeout=float(timeout_sec) + 2.0,
        )
    except EdgeBrokerError:
        raise
    except asyncio.TimeoutError:
        _raise("edge_broker_timeout")
    except Exception:
        _raise("edge_broker_unavailable")
    if not isinstance(response, dict):
        _raise("edge_broker_invalid_response")

    phase = response.get("phase")
    error_code = response.get("error_code")
    error_at_utc = response.get("error_at_utc")
    cdp_success = response.get("cdp_connected_success")
    cdp_connected = response.get("cdp_connected")
    broker_pid = response.get("broker_pid")
    mode = response.get("mode")
    if (
        phase not in _PHASES
        or mode != "dedicated"
        or (error_code is not None and (not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code)))
        or (error_at_utc is not None and (not isinstance(error_at_utc, str) or not _SAFE_UTC_TIMESTAMP.fullmatch(error_at_utc)))
        or (cdp_success is not None and type(cdp_success) is not bool)
        or type(cdp_connected) is not bool
        or (broker_pid is not None and (type(broker_pid) is not int or broker_pid <= 0))
        or response.get("status_ipc_supported") is not True
    ):
        _raise("edge_broker_invalid_response")
    return {
        "phase": phase,
        "mode": mode,
        "error_code": error_code,
        "error_at_utc": error_at_utc,
        "cdp_connected_success": cdp_success,
        "cdp_connected": cdp_connected,
        "broker_pid": broker_pid,
        "status_ipc_supported": True,
    }


async def get_browser_endpoint(timeout_sec: float = 30) -> tuple[str, str]:
    """Ask the authenticated per-user broker for its already-bound browser.

    Returns the loopback-only Playwright WebSocket endpoint and safe version. It
    never opens a CDP connection in the AoiTalk manager process.
    """
    if (
        type(timeout_sec) not in (int, float)
        or not math.isfinite(timeout_sec)
        or not 1 <= timeout_sec <= _MAX_TIMEOUT_SEC
    ):
        _raise("edge_broker_invalid_timeout")
    try:
        address, authkey = await asyncio.to_thread(_read_control_config)
        response = await asyncio.wait_for(
            asyncio.to_thread(_request_control_sync, address, authkey, float(timeout_sec)),
            timeout=float(timeout_sec) + 2.0,
        )
    except EdgeBrokerError:
        raise
    except asyncio.TimeoutError:
        _raise("edge_broker_timeout")
    except Exception:
        # Includes IPC errors whose text might contain the private pipe name.
        _raise("edge_broker_unavailable")
    if not isinstance(response, dict):
        _raise("edge_broker_invalid_response")
    phase = response.get("phase")
    if phase == "connecting":
        _raise("edge_broker_connecting")
    if phase == "failed":
        code = response.get("error")
        if isinstance(code, str) and re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code):
            _raise(code)
        _raise("edge_broker_failed")
    if phase != "ready":
        _raise("edge_broker_invalid_response")
    endpoint = _validate_loopback_ws_endpoint(response.get("endpoint"))
    if endpoint.casefold() == address.casefold():
        _raise("edge_broker_invalid_endpoint")
    version = _safe_browser_version(response.get("browser_version"))
    if not version:
        _raise("edge_broker_invalid_response")
    return endpoint, version


def _write_control_config(paths: BrokerPaths, address: str) -> None:
    address = _validate_pipe_address(address)
    _atomic_json_write(
        paths.control_file,
        {"version": _CONTROL_PROTOCOL_VERSION, "pipe": address},
    )


def _acquire_single_instance_lock(directory: Path) -> Any:
    if os.name != "nt":
        _raise("edge_broker_windows_required")
    import msvcrt

    try:
        stream = (directory / "broker.lock").open("a+b")
    except OSError:
        _raise("edge_broker_storage_unavailable")
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            stream.close()
        except OSError:
            pass
        _raise("edge_broker_already_running")
    return stream


def _release_single_instance_lock(stream: Any) -> None:
    import msvcrt

    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    stream.close()


def reset_state() -> None:
    """Clear persisted broker diagnostics while the broker is stopped."""
    paths = _paths()
    if not paths.directory.exists():
        return
    lock = _acquire_single_instance_lock(paths.directory)
    try:
        try:
            paths.state_file.unlink(missing_ok=True)
        except OSError:
            _raise("edge_broker_storage_unavailable")
    finally:
        try:
            _release_single_instance_lock(lock)
        except Exception:
            pass


async def _handle_client(connection: Any, core: EdgeBrokerCore) -> None:
    try:
        try:
            request = await asyncio.to_thread(connection.recv)
        except Exception:
            return
        if not isinstance(request, dict):
            response: dict[str, Any] = {"phase": "failed", "error": "edge_broker_invalid_request"}
        elif request.get("op") == "status":
            # Diagnostics only: do not call ensure_browser or mutate broker state.
            response = core.status()
        elif request.get("op") == "ensure_browser":
            wait_sec = request.get("wait_sec", 0)
            if type(wait_sec) not in (int, float) or not math.isfinite(wait_sec):
                response = {"phase": "failed", "error": "edge_broker_invalid_timeout"}
            else:
                response = await core.ensure_browser(float(wait_sec))
        else:
            response = {"phase": "failed", "error": "edge_broker_invalid_request"}
        try:
            await asyncio.to_thread(connection.send, response)
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


async def _accept_authenticated_client(listener: Any) -> Any | None:
    """Reject bad or disconnected clients without taking down the broker.

    Authentication failures and EOF are properties of an individual pipe
    client. Other accept errors indicate that the listener itself is unhealthy;
    fail once with a fixed code instead of retrying in a tight loop.
    """
    try:
        return await asyncio.to_thread(listener.accept)
    except (multiprocessing.connection.AuthenticationError, EOFError):
        return None
    except Exception:
        _raise("edge_broker_listener_failed")


async def serve() -> None:
    """Run the independent per-user Windows broker until its owner stops it."""
    diagnostics = None
    try:
        diagnostics = _BrokerDiagnostics(_paths().directory)
        diagnostics.start()
    except Exception:
        pass
    if not _enable_console_ctrl_c() and diagnostics is not None:
        try:
            diagnostics._emit("warning: console Ctrl+C initialization failed")
        except Exception:
            pass
    try:
        await _serve_with_diagnostics(diagnostics)
    except EdgeBrokerError as exc:
        if diagnostics is not None:
            diagnostics.transition("failed", exc.code)
        raise
    except Exception:
        if diagnostics is not None:
            diagnostics.transition("failed", "edge_broker_unavailable")
        raise


async def _serve_with_diagnostics(diagnostics: _BrokerDiagnostics | None) -> None:
    if os.name != "nt":
        _raise("edge_broker_windows_required")
    paths = _paths()
    try:
        paths.directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        _raise("edge_broker_storage_unavailable")
    instance_lock = _acquire_single_instance_lock(paths.directory)
    authkey = _load_or_create_authkey(paths)
    state = BrokerState(paths.state_file, diagnostics)
    dedicated_edge = DedicatedEdge()
    core = EdgeBrokerCore(state, opener=_dedicated_edge_opener(dedicated_edge))
    address = _validate_pipe_address(_pipe_address())
    listener = None
    try:
        listener = multiprocessing.connection.Listener(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        _write_control_config(paths, address)
        tasks: set[asyncio.Task[None]] = set()
        while True:
            connection = await _accept_authenticated_client(listener)
            if connection is None:
                continue
            task = asyncio.create_task(_handle_client(connection, core))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        await core.shutdown()
        try:
            _release_single_instance_lock(instance_lock)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="src.services.local_browser.cdp_broker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--reset-state", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        if args.reset_state:
            reset_state()
        elif args.status:
            print(json.dumps(asyncio.run(get_broker_status()), sort_keys=True, separators=(",", ":")))
        else:
            asyncio.run(serve())
    except EdgeBrokerError as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        # Driver, pipe, or operating-system errors can include private paths.
        print("edge_broker_unavailable", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
