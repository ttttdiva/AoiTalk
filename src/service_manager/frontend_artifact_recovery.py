"""Explicit, owner-coupled recovery of one existing Windows Next artifact.

This module is intentionally independent from normal service startup.  It does
not build or copy frontend files, read secrets from disk, change normal startup
guards, or touch the backend.  Its only entry point is the explicit CLI flag
``--recover-existing-artifact``.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import psutil

from src.utils.log_layout import LogLayout


EXPECTED_BUILD_ID = "nBfsHt1uMfzmZtueSvk_n"
EXPECTED_NEXT_PID = 61068
EXPECTED_OWNER_PID = 31752
EXPECTED_NEXT_PORT = 3002
EXPECTED_OWNER_PORT = 3000
EXPECTED_FILES_CHUNK_COUNT = 32
EXPECTED_FILES_ROUTE = "/(app)/filer/page"
FILES_MANIFEST_RELATIVE = Path("server") / "app" / "(app)" / "filer" / "page_client-reference-manifest.js"
REQUIRED_CHUNKS = frozenset({"1wl2arw6cs69y.js", "3ls66nkmho-s9.js"})
RECOVERY_MODE = "existing-artifact-reload"
RECEIPT_NAME = "frontend-existing-artifact-recovery.json"
LOCK_NAME_PREFIX = "Local\\AoiTalk.FrontendArtifactRecovery."
class RecoveryError(RuntimeError):
    """A fail-closed recovery error with no process environment values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileDigest:
    size: int
    sha256: str
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class ArtifactSnapshot:
    build_id: str
    build_id_digest: FileDigest
    files_manifest_sha256: str
    chunks: tuple[tuple[str, FileDigest], ...]

    def receipt(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "build_id_sha256": self.build_id_digest.sha256,
            "files_manifest_sha256": self.files_manifest_sha256,
            "files_chunk_count": len(self.chunks),
            "chunks": [
                {"name": name, "size": digest.size, "sha256": digest.sha256}
                for name, digest in self.chunks
            ],
        }


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    executable: str
    cwd: str
    command_line: tuple[str, ...]

    def receipt(self) -> dict[str, Any]:
        # Do not persist command lines: identity is PID + creation time, while
        # executable/cwd provide reviewable, secret-free context.
        return {
            "pid": self.pid,
            "create_time": self.create_time,
            "executable": self.executable,
            "cwd": self.cwd,
        }


@dataclass(frozen=True)
class EnvironmentContract:
    next_dist_dir: str
    node_env: str
    internal_api_key_present: bool
    python_api_url_contract: str
    gateway_activation_secret_present: bool
    login_trust_proxy: str

    def receipt(self) -> dict[str, Any]:
        return asdict(self)


def _canonical(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _same_identity(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return (
        left.pid == right.pid
        and math.isclose(left.create_time, right.create_time, rel_tol=0.0, abs_tol=0.01)
        and left.executable == right.executable
        and left.cwd == right.cwd
        and left.command_line == right.command_line
    )


def _read_process_identity(pid: int) -> ProcessIdentity:
    try:
        process = psutil.Process(int(pid))
        if not process.is_running():
            raise RecoveryError("process_not_running")
        created = float(process.create_time())
        executable = _canonical(process.exe())
        cwd = _canonical(process.cwd())
        command_line = tuple(str(item) for item in process.cmdline())
        if created <= 0 or not math.isfinite(created) or not executable or not cwd:
            raise RecoveryError("process_identity_unavailable")
        return ProcessIdentity(int(process.pid), created, executable, cwd, command_line)
    except RecoveryError:
        raise
    except (psutil.Error, OSError, TypeError, ValueError, OverflowError):
        raise RecoveryError("process_identity_unavailable") from None


def _read_process_environment(pid: int) -> dict[str, str]:
    """Read the live process environment into memory; never emit or persist it."""
    try:
        values = psutil.Process(int(pid)).environ()
    except (psutil.Error, OSError, TypeError, ValueError):
        raise RecoveryError("frontend_environment_unavailable") from None
    if not isinstance(values, Mapping) or not values:
        raise RecoveryError("frontend_environment_unavailable")
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RecoveryError("frontend_environment_invalid")
        result[key] = value
    return result


def _validate_environment_contract(environment: Mapping[str, str]) -> EnvironmentContract:
    """Validate only public/non-secret contract properties, retaining exact env."""
    required = (
        "NEXT_DIST_DIR",
        "NODE_ENV",
        "INTERNAL_API_KEY",
        "PYTHON_API_URL",
        "GATEWAY_SUPERVISOR_ACTIVATION_SECRET",
        "AOITALK_LOGIN_TRUST_PROXY",
    )
    if any(key not in environment for key in required):
        raise RecoveryError("frontend_environment_contract_missing")
    if environment["NEXT_DIST_DIR"] != ".next":
        raise RecoveryError("frontend_environment_contract_invalid")
    if environment["NODE_ENV"] != "production":
        raise RecoveryError("frontend_environment_contract_invalid")
    if not environment["INTERNAL_API_KEY"]:
        raise RecoveryError("frontend_environment_contract_invalid")
    if not environment["GATEWAY_SUPERVISOR_ACTIVATION_SECRET"]:
        raise RecoveryError("frontend_environment_contract_invalid")
    trust_proxy = environment["AOITALK_LOGIN_TRUST_PROXY"].strip().lower()
    if trust_proxy not in {"true", "false"}:
        raise RecoveryError("frontend_environment_contract_invalid")
    try:
        from urllib.parse import urlsplit

        api_url = urlsplit(environment["PYTHON_API_URL"])
        if (
            api_url.scheme != "http"
            or api_url.hostname != "127.0.0.1"
            or api_url.port != EXPECTED_OWNER_PORT
            or api_url.username is not None
            or api_url.password is not None
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise RecoveryError("frontend_environment_contract_invalid") from None
    return EnvironmentContract(
        next_dist_dir=".next",
        node_env="production",
        internal_api_key_present=True,
        python_api_url_contract="http-loopback-3000",
        gateway_activation_secret_present=True,
        login_trust_proxy=trust_proxy,
    )


def _file_digest(path: Path, *, boundary: Path) -> FileDigest:
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_boundary)
        lexical_boundary = Path(os.path.abspath(boundary))
        lexical_path = Path(os.path.abspath(path))
        relative = lexical_path.relative_to(lexical_boundary)
        current = lexical_boundary
        for part in relative.parts:
            current = current / part
            component = current.lstat()
            if stat.S_ISLNK(component.st_mode) or bool(
                getattr(component, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise RecoveryError("artifact_reparse_point_rejected")
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or bool(
            getattr(before, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise RecoveryError("artifact_reparse_point_rejected")
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("artifact_not_regular_file")
        content = path.read_bytes()
        after = path.stat()
    except RecoveryError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise RecoveryError("artifact_file_unreadable") from None
    first = (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_dev, before.st_ino)
    second = (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_dev, after.st_ino)
    if first != second or len(content) != after.st_size:
        raise RecoveryError("artifact_changed_during_read")
    return FileDigest(
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
        device=after.st_dev,
        inode=after.st_ino,
    )


def _files_manifest_payload(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise RecoveryError("files_manifest_unreadable") from None
    marker = f'globalThis.__RSC_MANIFEST["{EXPECTED_FILES_ROUTE}"] = '
    lines = [line for line in text.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise RecoveryError("files_manifest_route_missing_or_ambiguous")
    payload = lines[0][len(marker) :].strip()
    if payload.endswith(";"):
        payload = payload[:-1]
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise RecoveryError("files_manifest_invalid") from None
    if not isinstance(value, dict) or not isinstance(value.get("clientModules"), dict):
        raise RecoveryError("files_manifest_invalid")
    return content, value


def _files_chunk_names(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    names: set[str] = set()
    client_modules = manifest.get("clientModules")
    if not isinstance(client_modules, dict):
        raise RecoveryError("files_manifest_invalid")
    for record in client_modules.values():
        if not isinstance(record, dict):
            raise RecoveryError("files_manifest_invalid")
        chunks = record.get("chunks", [])
        if not isinstance(chunks, list):
            raise RecoveryError("files_manifest_invalid")
        for entry in chunks:
            if not isinstance(entry, str):
                raise RecoveryError("files_manifest_invalid")
            match = re.fullmatch(r"/at/_next/static/chunks/([A-Za-z0-9._-]+\.js)", entry)
            if match:
                name = match.group(1)
                if PurePosixPath(name).name != name:
                    raise RecoveryError("files_manifest_chunk_path_invalid")
                names.add(name)
            elif "/_next/static/chunks/" in entry:
                raise RecoveryError("files_manifest_chunk_path_invalid")
    if len(names) != EXPECTED_FILES_CHUNK_COUNT:
        raise RecoveryError("files_manifest_chunk_count_invalid")
    if not REQUIRED_CHUNKS.issubset(names):
        raise RecoveryError("required_chunk_not_referenced")
    return tuple(sorted(names))


def capture_artifact_snapshot(frontend_dir: Path, expected_build_id: str = EXPECTED_BUILD_ID) -> ArtifactSnapshot:
    """Read BUILD_ID, the Files manifest and its 32 chunk payloads without writes."""
    dist_dir = frontend_dir / ".next"
    try:
        build_bytes = (dist_dir / "BUILD_ID").read_bytes()
        build_id = build_bytes.decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise RecoveryError("build_id_unreadable") from None
    if build_id != expected_build_id:
        raise RecoveryError("build_id_mismatch")
    build_digest = _file_digest(dist_dir / "BUILD_ID", boundary=dist_dir)
    manifest_path = dist_dir / FILES_MANIFEST_RELATIVE
    manifest_bytes, manifest = _files_manifest_payload(manifest_path)
    manifest_digest = _file_digest(manifest_path, boundary=dist_dir)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_digest.sha256:
        raise RecoveryError("files_manifest_changed_during_read")
    names = _files_chunk_names(manifest)
    chunk_root = dist_dir / "static" / "chunks"
    chunks = tuple(
        (name, _file_digest(chunk_root / name, boundary=dist_dir)) for name in names
    )
    return ArtifactSnapshot(
        build_id=build_id,
        build_id_digest=build_digest,
        files_manifest_sha256=manifest_digest.sha256,
        chunks=chunks,
    )


def _validate_next_identity(identity: ProcessIdentity, frontend_dir: Path) -> None:
    if Path(identity.executable).name.casefold() != "node.exe":
        raise RecoveryError("next_executable_mismatch")
    if identity.cwd != _canonical(frontend_dir):
        raise RecoveryError("next_cwd_mismatch")
    args = tuple(item.casefold().replace("\\", "/") for item in identity.command_line)
    joined = " ".join(args)
    if not ("next" in joined and "start" in args and "3002" in args and "127.0.0.1" in args):
        raise RecoveryError("next_command_mismatch")
    if "-p" not in args or "-h" not in args:
        raise RecoveryError("next_command_mismatch")


def _validate_owner_identity(identity: ProcessIdentity, project_root: Path) -> None:
    if Path(identity.executable).name.casefold() not in {"python.exe", "pythonw.exe"}:
        raise RecoveryError("backend_executable_mismatch")
    if identity.cwd != _canonical(project_root):
        raise RecoveryError("backend_cwd_mismatch")
    if "main.py" not in " ".join(identity.command_line).casefold():
        raise RecoveryError("backend_command_mismatch")


def _is_listener_owned_by(pid: int, port: int) -> bool:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        raise RecoveryError("listener_inventory_unavailable") from None
    return any(
        conn.pid == pid
        and conn.status == psutil.CONN_LISTEN
        and getattr(conn.laddr, "port", None) == port
        for conn in connections
    )


def _listener_pids(port: int) -> set[int]:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        raise RecoveryError("listener_inventory_unavailable") from None
    return {
        int(conn.pid)
        for conn in connections
        if conn.pid is not None
        and conn.status == psutil.CONN_LISTEN
        and getattr(conn.laddr, "port", None) == port
    }


def _is_descendant_of(pid: int, owner_pid: int) -> bool:
    seen: set[int] = set()
    current = int(pid)
    for _ in range(32):
        if current == owner_pid:
            return True
        if current <= 0 or current in seen:
            return False
        seen.add(current)
        try:
            parent = int(psutil.Process(current).ppid())
        except (psutil.Error, OSError, ValueError):
            return False
        current = parent
    return False


class _WindowsApi:
    """Small Win32 boundary; handles are non-inheritable unless stated."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    CREATE_SUSPENDED = 0x00000004
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NO_WINDOW = 0x08000000
    STARTF_USESTDHANDLES = 0x00000100
    SW_HIDE = 0
    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x00000080
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    GENERIC_WRITE = 0x40000000
    FILE_APPEND_DATA = 0x00000004
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_ALWAYS = 4
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        pass

    EXTENDED_LIMIT_INFORMATION._fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    def __init__(self) -> None:
        if os.name != "nt":
            raise RecoveryError("windows_only")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        k = self.kernel
        k.CreateMutexW.argtypes = [ctypes.POINTER(self.SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.POINTER(self.SECURITY_ATTRIBUTES), wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(self.FILETIME), ctypes.POINTER(self.FILETIME), ctypes.POINTER(self.FILETIME), ctypes.POINTER(self.FILETIME)]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(self.SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(self.SECURITY_ATTRIBUTES), ctypes.POINTER(self.SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(self.STARTUPINFOW), ctypes.POINTER(self.PROCESS_INFORMATION)]
        k.CreateProcessW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k.ReleaseMutex.restype = wintypes.BOOL
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k.IsProcessInJob.restype = wintypes.BOOL

    @staticmethod
    def _handle_value(handle: Any) -> int:
        return int(ctypes.cast(handle, ctypes.c_void_p).value or 0)

    def create_singleton_mutex(self, name: str) -> int:
        handle = self.kernel.CreateMutexW(None, False, name)
        if not handle:
            raise RecoveryError("recovery_lock_create_failed")
        result = int(self.kernel.WaitForSingleObject(handle, 0))
        if result not in {self.WAIT_OBJECT_0, self.WAIT_ABANDONED}:
            self.kernel.CloseHandle(handle)
            raise RecoveryError("recovery_already_running")
        return self._handle_value(handle)

    def release_mutex(self, handle: int) -> None:
        if handle:
            self.kernel.ReleaseMutex(wintypes.HANDLE(handle))
            self.kernel.CloseHandle(wintypes.HANDLE(handle))

    def create_job(self) -> int:
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise RecoveryError("job_create_failed")
        info = self.EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job = self._handle_value(handle)
        try:
            if not self.kernel.SetInformationJobObject(
                handle, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise RecoveryError("job_kill_on_close_setup_failed")
            confirmed = self.EXTENDED_LIMIT_INFORMATION()
            if not self.kernel.QueryInformationJobObject(
                handle, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(confirmed), ctypes.sizeof(confirmed), None
            ):
                raise RecoveryError("job_kill_on_close_verify_failed")
            if confirmed.BasicLimitInformation.LimitFlags != self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
                raise RecoveryError("job_kill_on_close_verify_failed")
            return job
        except Exception:
            self.kernel.CloseHandle(handle)
            raise

    def open_process(self, pid: int, access: int) -> int:
        handle = self.kernel.OpenProcess(access, False, int(pid))
        if not handle:
            raise RecoveryError("process_handle_unavailable")
        return self._handle_value(handle)

    def process_create_time(self, handle: int) -> float:
        creation = self.FILETIME()
        exit_time = self.FILETIME()
        kernel_time = self.FILETIME()
        user_time = self.FILETIME()
        if not self.kernel.GetProcessTimes(
            wintypes.HANDLE(handle), ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)
        ):
            raise RecoveryError("process_handle_identity_unavailable")
        ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return ticks / 10_000_000 - 11644473600

    def process_handle_alive(self, handle: int) -> bool:
        result = int(self.kernel.WaitForSingleObject(wintypes.HANDLE(handle), 0))
        if result == self.WAIT_TIMEOUT:
            return True
        if result == self.WAIT_OBJECT_0:
            return False
        raise RecoveryError("process_handle_wait_failed")

    def assign(self, job: int, process_handle: int) -> None:
        if not self.kernel.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(process_handle)):
            raise RecoveryError("job_assignment_denied")

    def open_append_log(self, path: Path) -> int:
        security = self.SECURITY_ATTRIBUTES(ctypes.sizeof(self.SECURITY_ATTRIBUTES), None, True)
        handle = self.kernel.CreateFileW(
            str(path), self.FILE_APPEND_DATA | self.GENERIC_WRITE,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE, ctypes.byref(security),
            self.OPEN_ALWAYS, self.FILE_ATTRIBUTE_NORMAL, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if self._handle_value(handle) in {0, invalid}:
            raise RecoveryError("frontend_log_open_failed")
        return self._handle_value(handle)

    def open_nul_input(self) -> int:
        security = self.SECURITY_ATTRIBUTES(ctypes.sizeof(self.SECURITY_ATTRIBUTES), None, True)
        handle = self.kernel.CreateFileW(
            "NUL", self.GENERIC_READ,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE, ctypes.byref(security),
            self.OPEN_EXISTING, self.FILE_ATTRIBUTE_NORMAL, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if self._handle_value(handle) in {0, invalid}:
            raise RecoveryError("stdin_handle_unavailable")
        return self._handle_value(handle)

    def create_suspended_next(
        self, *, environment: Mapping[str, str], frontend_dir: Path, log_path: Path
    ) -> tuple[int, int, int]:
        normalized = {key.casefold(): value for key, value in environment.items()}
        system_root = normalized.get("systemroot")
        comspec = normalized.get("comspec")
        command_shell = comspec or (str(Path(system_root) / "System32" / "cmd.exe") if system_root else "")
        if not command_shell or not Path(command_shell).is_file():
            raise RecoveryError("command_shell_unavailable")
        log_handle = self.open_append_log(log_path)
        input_handle = None
        try:
            input_handle = self.open_nul_input()
            startup = self.STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            startup.dwFlags = self.STARTF_USESTDHANDLES
            startup.wShowWindow = self.SW_HIDE
            startup.hStdInput = wintypes.HANDLE(input_handle)
            startup.hStdOutput = wintypes.HANDLE(log_handle)
            startup.hStdError = wintypes.HANDLE(log_handle)
            process_info = self.PROCESS_INFORMATION()
            command = f'"{command_shell}" /d /s /c "npm run start -- -p 3002 -H 127.0.0.1"'
            command_buffer = ctypes.create_unicode_buffer(command)
            environment_block = _create_environment_block(environment)
            created = self.kernel.CreateProcessW(
                command_shell,
                command_buffer,
                None,
                None,
                True,
                self.CREATE_SUSPENDED | self.CREATE_UNICODE_ENVIRONMENT | self.CREATE_NO_WINDOW,
                ctypes.cast(environment_block, ctypes.c_void_p),
                str(frontend_dir),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
            if not created:
                raise RecoveryError("next_process_create_failed")
            return (
                self._handle_value(process_info.hProcess),
                self._handle_value(process_info.hThread),
                int(process_info.dwProcessId),
            )
        finally:
            if input_handle:
                self.kernel.CloseHandle(wintypes.HANDLE(input_handle))
            self.kernel.CloseHandle(wintypes.HANDLE(log_handle))

    def resume(self, thread_handle: int) -> None:
        if int(self.kernel.ResumeThread(wintypes.HANDLE(thread_handle))) == self.WAIT_FAILED:
            raise RecoveryError("next_process_resume_failed")

    def terminate(self, process_handle: int) -> None:
        if not self.kernel.TerminateProcess(wintypes.HANDLE(process_handle), 0):
            raise RecoveryError("owned_process_termination_failed")

    def wait(self, handle: int, timeout_ms: int) -> int:
        return int(self.kernel.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms))

    def is_process_in_job(self, pid: int, job: int) -> bool:
        process_handle = self.open_process(pid, self.PROCESS_QUERY_LIMITED_INFORMATION)
        try:
            in_job = wintypes.BOOL()
            if not self.kernel.IsProcessInJob(
                wintypes.HANDLE(process_handle), wintypes.HANDLE(job), ctypes.byref(in_job)
            ):
                raise RecoveryError("job_membership_check_failed")
            return bool(in_job.value)
        finally:
            self.close_handle(process_handle)

    def close_handle(self, handle: int | None) -> None:
        if handle:
            self.kernel.CloseHandle(wintypes.HANDLE(handle))


def _owner_alive(api: _WindowsApi, handle: int, expected: ProcessIdentity) -> bool:
    if not api.process_handle_alive(handle):
        return False
    try:
        current = _read_process_identity(expected.pid)
    except RecoveryError:
        return False
    return (
        current.pid == expected.pid
        and math.isclose(current.create_time, expected.create_time, rel_tol=0.0, abs_tol=0.01)
        and current.executable == expected.executable
        and current.cwd == expected.cwd
        and current.command_line == expected.command_line
    )


def _check_handle_matches(api: _WindowsApi, handle: int, expected: ProcessIdentity) -> None:
    if not api.process_handle_alive(handle):
        raise RecoveryError("process_identity_changed")
    handle_created = api.process_create_time(handle)
    if not math.isclose(handle_created, expected.create_time, rel_tol=0.0, abs_tol=0.02):
        raise RecoveryError("process_identity_changed")
    current = _read_process_identity(expected.pid)
    if not _same_identity(current, expected):
        raise RecoveryError("process_identity_changed")


def _atomic_write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RecoveryError("receipt_write_failed") from None


def _create_environment_block(environment: Mapping[str, str]) -> ctypes.Array[Any]:
    entries: list[str] = []
    seen_names: set[str] = set()
    for key, value in sorted(environment.items(), key=lambda pair: pair[0].casefold()):
        if not key or "\0" in key or "\0" in value or ("=" in key and not key.startswith("=")):
            raise RecoveryError("frontend_environment_invalid")
        folded = key.casefold()
        if folded in seen_names:
            raise RecoveryError("frontend_environment_invalid")
        seen_names.add(folded)
        entries.append(f"{key}={value}")
    if not entries:
        raise RecoveryError("frontend_environment_invalid")
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


def _wait_for_port(
    api: _WindowsApi,
    *,
    port: int,
    job: int,
    frontend_dir: Path,
    timeout: float,
    owner_handle: int,
    owner: ProcessIdentity,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _owner_alive(api, owner_handle, owner):
            raise RecoveryError("backend_owner_ended_during_start")
        listeners = _listener_pids(port)
        if listeners:
            if len(listeners) != 1:
                raise RecoveryError("unexpected_listener_on_frontend_port")
            pid = next(iter(listeners))
            identity = _read_process_identity(pid)
            if Path(identity.executable).name.casefold() != "node.exe" or identity.cwd != _canonical(frontend_dir):
                raise RecoveryError("unexpected_listener_on_frontend_port")
            if not api.is_process_in_job(pid, job):
                raise RecoveryError("frontend_listener_not_job_owned")
            return pid
        time.sleep(0.25)
    raise RecoveryError("frontend_listener_start_timeout")


def _wait_for_port_release(*, port: int, owner_handle: int, owner: ProcessIdentity, api: _WindowsApi, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _owner_alive(api, owner_handle, owner):
            raise RecoveryError("backend_owner_ended_before_resume")
        if not _listener_pids(port):
            return
        time.sleep(0.2)
    raise RecoveryError("frontend_port_not_released")


def _environment_contract_receipt(environment: Mapping[str, str]) -> dict[str, Any]:
    contract = _validate_environment_contract(environment)
    return contract.receipt()


def run_existing_artifact_recovery(
    *,
    project_root: Path | None = None,
    api: _WindowsApi | None = None,
    output: Callable[[str], None] = print,
) -> None:
    """Replace only the pinned Next process and supervise it with backend identity."""
    if os.name != "nt":
        raise RecoveryError("windows_only")
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    frontend_dir = root / "frontend"
    if api is None:
        api = _WindowsApi()
    if _canonical(sys.executable) != _canonical(root / "venv" / "Scripts" / "python.exe"):
        raise RecoveryError("existing_venv_required")

    mutex_material = hashlib.sha256(_canonical(root).encode("utf-8")).hexdigest()[:20]
    mutex = api.create_singleton_mutex(LOCK_NAME_PREFIX + mutex_material)
    owner_handle = next_handle = next_thread = job = old_handle = None
    child_created = False
    receipt_path = LogLayout(root).startup_dir / RECEIPT_NAME
    frontend_log_path = LogLayout(root).frontend_log_path()
    stage = "preflight"
    owner: ProcessIdentity | None = None
    old_next: ProcessIdentity | None = None
    new_pid: int | None = None
    artifact: ArtifactSnapshot | None = None
    env_contract: dict[str, Any] | None = None
    job_closed = False
    try:
        owner = _read_process_identity(EXPECTED_OWNER_PID)
        _validate_owner_identity(owner, root)
        if not _is_listener_owned_by(owner.pid, EXPECTED_OWNER_PORT):
            raise RecoveryError("backend_listener_identity_mismatch")
        owner_handle = api.open_process(
            owner.pid, api.SYNCHRONIZE | api.PROCESS_QUERY_LIMITED_INFORMATION
        )
        _check_handle_matches(api, owner_handle, owner)

        old_next = _read_process_identity(EXPECTED_NEXT_PID)
        _validate_next_identity(old_next, frontend_dir)
        if not _is_descendant_of(old_next.pid, owner.pid):
            raise RecoveryError("next_not_owned_by_backend_tree")
        if not _is_listener_owned_by(old_next.pid, EXPECTED_NEXT_PORT):
            raise RecoveryError("next_listener_identity_mismatch")
        old_handle = api.open_process(
            old_next.pid,
            api.SYNCHRONIZE | api.PROCESS_TERMINATE | api.PROCESS_QUERY_LIMITED_INFORMATION,
        )
        _check_handle_matches(api, old_handle, old_next)

        environment = _read_process_environment(old_next.pid)
        env_contract = _environment_contract_receipt(environment)
        before_artifact = capture_artifact_snapshot(frontend_dir)
        job = api.create_job()

        frontend_log_path.parent.mkdir(parents=True, exist_ok=True)
        stage = "next_suspended"
        next_handle, next_thread, new_pid = api.create_suspended_next(
            environment=environment,
            frontend_dir=frontend_dir,
            log_path=frontend_log_path,
        )
        api.assign(job, next_handle)
        child_created = True

        after_artifact = capture_artifact_snapshot(frontend_dir)
        if before_artifact != after_artifact:
            raise RecoveryError("artifact_changed_during_preparation")
        artifact = after_artifact
        _atomic_write_receipt(
            receipt_path,
            {
                "mode": RECOVERY_MODE,
                "status": "prepared_suspended",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "artifact": artifact.receipt(),
                "environment_contract": env_contract,
                "owner": owner.receipt(),
                "old_next": old_next.receipt(),
                "supervisor": {"pid": os.getpid(), "create_time": psutil.Process(os.getpid()).create_time()},
                "next_launcher_pid": new_pid,
                "ownership": {
                    "owner_tracking": "retained_backend_process_handle_and_creation_time",
                    "cleanup": "noninheritable_windows_job_kill_on_close",
                    "backend_child_list_registered": False,
                },
            },
        )
        _check_handle_matches(api, owner_handle, owner)
        _check_handle_matches(api, old_handle, old_next)
        current_next = _read_process_identity(old_next.pid)
        _validate_next_identity(current_next, frontend_dir)
        if not _same_identity(current_next, old_next):
            raise RecoveryError("next_identity_changed_before_stop")
        if not _is_listener_owned_by(old_next.pid, EXPECTED_NEXT_PORT):
            raise RecoveryError("next_listener_identity_changed_before_stop")

        stage = "old_next_stopping"
        api.terminate(old_handle)
        wait_result = api.wait(old_handle, 15000)
        if wait_result != api.WAIT_OBJECT_0:
            raise RecoveryError("old_next_stop_timeout")
        if not _owner_alive(api, owner_handle, owner):
            raise RecoveryError("backend_owner_ended_before_resume")
        _wait_for_port_release(
            port=EXPECTED_NEXT_PORT,
            owner_handle=owner_handle,
            owner=owner,
            api=api,
            timeout=15,
        )

        stage = "replacement_starting"
        api.resume(next_thread)
        api.close_handle(next_thread)
        next_thread = None
        listener_pid = _wait_for_port(
            api,
            port=EXPECTED_NEXT_PORT,
            job=job,
            frontend_dir=frontend_dir,
            timeout=45,
            owner_handle=owner_handle,
            owner=owner,
        )
        new_identity = _read_process_identity(listener_pid)
        _validate_next_identity(new_identity, frontend_dir)
        final_artifact = capture_artifact_snapshot(frontend_dir)
        if artifact != final_artifact:
            raise RecoveryError("artifact_changed_after_start")
        _check_handle_matches(api, owner_handle, owner)
        payload: dict[str, Any] = {
            "mode": RECOVERY_MODE,
            "status": "running",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "artifact": final_artifact.receipt(),
            "environment_contract": env_contract,
            "owner": owner.receipt(),
            "old_next": old_next.receipt(),
            "supervisor": {
                "pid": os.getpid(),
                "create_time": psutil.Process(os.getpid()).create_time(),
            },
            "new_next": new_identity.receipt(),
            "next_launcher_pid": new_pid,
            "ownership": {
                "owner_tracking": "retained_backend_process_handle_and_creation_time",
                "cleanup": "noninheritable_windows_job_kill_on_close",
                "backend_child_list_registered": False,
            },
            "startup_result": "listener_ready_on_127.0.0.1:3002",
        }
        _atomic_write_receipt(receipt_path, payload)
        stage = "monitoring"
        output(f"FRONTEND_RECOVERY_RUNNING next_pid={listener_pid} build_id={final_artifact.build_id}")

        while True:
            if not _owner_alive(api, owner_handle, owner):
                payload["status"] = "stopped_owner_exit_or_identity_change"
                payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
                payload["startup_result"] = "owner_identity_no_longer_valid"
                api.close_handle(job)
                job_closed = True
                payload["cleanup_result"] = "owned_next_job_closed"
                _atomic_write_receipt(receipt_path, payload)
                output("FRONTEND_RECOVERY_STOPPED owner_identity_no_longer_valid")
                return
            current_listeners = _listener_pids(EXPECTED_NEXT_PORT)
            if current_listeners != {listener_pid}:
                payload["status"] = "stopped_next_listener_lost"
                payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
                payload["startup_result"] = (
                    "next_listener_lost"
                    if not current_listeners
                    else "frontend_port_listener_identity_changed"
                )
                api.close_handle(job)
                job_closed = True
                payload["cleanup_result"] = "owned_next_job_closed"
                _atomic_write_receipt(receipt_path, payload)
                output(f"FRONTEND_RECOVERY_STOPPED {payload['startup_result']}")
                return
            current_listener = _read_process_identity(listener_pid)
            if not _same_identity(current_listener, new_identity) or not api.is_process_in_job(listener_pid, job):
                payload["status"] = "stopped_next_identity_changed"
                payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
                payload["startup_result"] = "next_listener_identity_changed"
                api.close_handle(job)
                job_closed = True
                payload["cleanup_result"] = "owned_next_job_closed"
                _atomic_write_receipt(receipt_path, payload)
                output("FRONTEND_RECOVERY_STOPPED next_listener_identity_changed")
                return
            time.sleep(1.0)
    except RecoveryError as exc:
        if receipt_path.parent.exists():
            failure: dict[str, Any] = {
                "mode": RECOVERY_MODE,
                "status": "failed",
                "stage": stage,
                "error_code": exc.code,
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "supervisor": {"pid": os.getpid()},
            }
            if owner is not None:
                failure["owner"] = owner.receipt()
            if old_next is not None:
                failure["old_next"] = old_next.receipt()
            if artifact is not None:
                failure["artifact"] = artifact.receipt()
            if env_contract is not None:
                failure["environment_contract"] = env_contract
            try:
                _atomic_write_receipt(receipt_path, failure)
            except RecoveryError:
                pass
        if job and not job_closed:
            # Closing this non-inheritable job terminates only the replacement
            # that this recovery prepared; the backend and old wrappers are
            # never members of it.
            api.close_handle(job)
            job_closed = True
        if next_handle and not child_created:
            try:
                api.terminate(next_handle)
            except RecoveryError:
                pass
        output(f"FRONTEND_RECOVERY_FAILED code={exc.code} stage={stage}")
        raise
    finally:
        if next_thread:
            api.close_handle(next_thread)
        if next_handle:
            api.close_handle(next_handle)
        if job and not job_closed:
            # On normal Python interruption/exception, Job close is the final
            # orphan-prevention mechanism.
            api.close_handle(job)
        if old_handle:
            api.close_handle(old_handle)
        if owner_handle:
            api.close_handle(owner_handle)
        api.release_mutex(mutex)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Existing-artifact frontend recovery")
    parser.add_argument(
        "--recover-existing-artifact",
        action="store_true",
        help="replace only the pinned Next process and bind its lifetime to backend PID 31752",
    )
    args = parser.parse_args(argv)
    if not args.recover_existing_artifact:
        parser.error("--recover-existing-artifact is required")
    try:
        run_existing_artifact_recovery()
        return 0
    except RecoveryError:
        return 2
    except KeyboardInterrupt:
        # The supervisor's job handle closes in finally, killing only its Next
        # subtree.  Do not print exception strings or environment data.
        return 130
    except Exception:
        # Unexpected API/library errors are not printed: exception text can
        # contain values from the inherited environment.
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
