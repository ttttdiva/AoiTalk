"""OS-level isolation helpers for server-side App Jobs.

The runner owns only processes it spawned and never kills unrelated PIDs.
When the deployment cannot satisfy the isolation contract, callers must
fail closed instead of falling back to a cwd-only pseudo sandbox.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID


logger = logging.getLogger(__name__)

# Windows Job Object limit flags (winnt.h)
_WIN_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WIN_LIMIT_PROCESS_MEMORY = 0x00000100
_WIN_LIMIT_JOB_MEMORY = 0x00000200
_WIN_LIMIT_ACTIVE_PROCESS = 0x00000008
_WIN_CREATE_SUSPENDED = 0x00000004
_WIN_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004

_BWRAP_HOST_RO_BINDS = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/ca-certificates",
)
_BWRAP_LAUNCHER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")


class AppJobIsolationError(RuntimeError):
    """Isolation backend is missing or cannot satisfy the contract."""


@dataclass(frozen=True)
class IsolationContract:
    backend: str
    network_isolated: bool
    memory_limited: bool
    pid_scoped: bool
    file_scoped: bool


@dataclass
class _OwnedProcess:
    process: subprocess.Popen[str]
    backend: str
    windows_job_handle: int | None = None
    root_pid: int | None = None


_OWNED: dict[str, _OwnedProcess] = {}
_LOCK = threading.Lock()


def _apps_jobs_isolation_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    apps = config.get("apps")
    if not isinstance(apps, dict):
        return {}
    jobs = apps.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    isolation = jobs.get("isolation")
    return isolation if isinstance(isolation, dict) else {}


def _memory_limit_bytes(config: Mapping[str, Any] | None) -> int:
    isolation = _apps_jobs_isolation_config(config)
    raw_mb = isolation.get("memory_limit_mb", 512)
    try:
        mb = int(raw_mb)
    except (TypeError, ValueError):
        mb = 512
    return max(64, min(mb, 16_384)) * 1024 * 1024


def _require_network_isolation(config: Mapping[str, Any] | None) -> bool:
    isolation = _apps_jobs_isolation_config(config)
    if "require_network_isolation" in isolation:
        return bool(isolation.get("require_network_isolation"))
    # Fail closed on POSIX when bubblewrap is absent; Windows job objects do not
    # provide network isolation without an external runner.
    return os.name != "nt"


def detect_isolation_contract(
    config: Mapping[str, Any] | None = None,
) -> IsolationContract | None:
    """Return the best available backend or None when unsafe to run."""
    if shutil.which("bwrap"):
        return IsolationContract(
            backend="bubblewrap",
            network_isolated=True,
            memory_limited=False,
            pid_scoped=True,
            file_scoped=True,
        )
    if os.name == "nt":
        return IsolationContract(
            backend="windows_job",
            network_isolated=False,
            memory_limited=True,
            pid_scoped=True,
            file_scoped=False,
        )
    if os.name == "posix":
        return IsolationContract(
            backend="posix_rlimit",
            network_isolated=False,
            memory_limited=True,
            pid_scoped=True,
            file_scoped=False,
        )
    return None


def require_isolation_contract(
    config: Mapping[str, Any] | None = None,
) -> IsolationContract:
    contract = detect_isolation_contract(config)
    if contract is None:
        raise AppJobIsolationError("App Job 用の隔離 backend を利用できません")
    if _require_network_isolation(config) and not contract.network_isolated:
        raise AppJobIsolationError(
            "この環境ではネットワーク隔離付きの App Job runner を提供できません"
        )
    return contract


def _create_windows_job(process: subprocess.Popen[str], *, memory_limit_bytes: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _WIN_LIMIT_KILL_ON_JOB_CLOSE
            | _WIN_LIMIT_PROCESS_MEMORY
            | _WIN_LIMIT_JOB_MEMORY
            | _WIN_LIMIT_ACTIVE_PROCESS
        )
        info.BasicLimitInformation.ActiveProcessLimit = 64
        info.ProcessMemoryLimit = ctypes.c_size_t(memory_limit_bytes)
        info.JobMemoryLimit = ctypes.c_size_t(memory_limit_bytes)
        configured = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job,
            wintypes.HANDLE(int(process._handle)),
        )
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        logger.debug("Windows job object setup failed", exc_info=True)
        return None


def _resume_windows_process(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.restype = wintypes.DWORD
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    invalid_handle = int(wintypes.HANDLE(-1).value)
    if not snapshot or int(snapshot) == invalid_handle:
        raise AppJobIsolationError("Windows プロセスの再開に失敗しました")
    thread_id: int | None = None
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(THREADENTRY32)
    try:
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == process.pid:
                    thread_id = int(entry.th32ThreadID)
                    break
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    if thread_id is None:
        raise AppJobIsolationError("Windows プロセスの再開に失敗しました")
    thread_handle = kernel32.OpenThread(_WIN_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread_handle:
        raise AppJobIsolationError("Windows プロセスの再開に失敗しました")
    try:
        resumed = kernel32.ResumeThread(wintypes.HANDLE(thread_handle))
        if resumed == 0xFFFFFFFF:
            raise AppJobIsolationError("Windows プロセスの再開に失敗しました")
    finally:
        kernel32.CloseHandle(thread_handle)


def _close_windows_job(handle: int | None) -> None:
    if os.name != "nt" or not handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            wintypes.HANDLE(handle)
        )
    except Exception:
        logger.debug("Failed to close Windows job handle", exc_info=True)


def _posix_preexec(memory_limit_bytes: int) -> Any:
    import resource

    def _apply() -> None:
        try:
            os.setsid()
        except OSError:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        except (OSError, ValueError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (86_400, 86_400))
        except (OSError, ValueError):
            pass

    return _apply


def _bwrap_launcher_env() -> dict[str, str]:
    return {
        env_key: os.environ[env_key]
        for env_key in _BWRAP_LAUNCHER_ENV_KEYS
        if env_key in os.environ
    }


def _bubblewrap_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise AppJobIsolationError("bubblewrap が見つかりません")
    cmd = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-net",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for host_path in _BWRAP_HOST_RO_BINDS:
        path = Path(host_path)
        if path.exists():
            cmd.extend(["--ro-bind", str(path), str(path)])
    cmd.extend(
        [
            "--bind",
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
        ]
    )
    for env_name, env_value in env.items():
        cmd.extend(["--setenv", env_name, env_value])
    cmd.append("--")
    cmd.extend(argv)
    return cmd


def spawn_isolated_process(
    *,
    job_id: str | UUID,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    log_file: Any,
    config: Mapping[str, Any] | None = None,
) -> subprocess.Popen[str]:
    """Start one isolated child process owned by this worker."""
    owned_key = str(job_id)
    contract = require_isolation_contract(config)
    memory_limit = _memory_limit_bytes(config)
    cwd = cwd.resolve()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if contract.backend == "windows_job":
            creationflags |= _WIN_CREATE_SUSPENDED
    start_new_session = os.name != "nt" and contract.backend != "bubblewrap"

    launch_argv = argv
    launch_cwd = cwd
    preexec = None
    if contract.backend == "bubblewrap":
        launch_argv = _bubblewrap_command(argv, cwd=cwd, env=env)
        launch_cwd = cwd
        child_env = _bwrap_launcher_env()
    else:
        child_env = dict(env)
        if contract.backend == "posix_rlimit":
            preexec = _posix_preexec(memory_limit)

    process = subprocess.Popen(
        launch_argv,
        cwd=launch_cwd,
        shell=False,
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        creationflags=creationflags,
        start_new_session=start_new_session,
        preexec_fn=preexec,
    )
    windows_job = None
    if contract.backend == "windows_job":
        windows_job = _create_windows_job(process, memory_limit_bytes=memory_limit)
        if windows_job is None:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)
            raise AppJobIsolationError(
                "Windows Job Object への割当に失敗したため App Job を開始できません"
            )
        try:
            _resume_windows_process(process)
        except AppJobIsolationError:
            _close_windows_job(windows_job)
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)
            raise
    with _LOCK:
        _OWNED[owned_key] = _OwnedProcess(
            process=process,
            backend=contract.backend,
            windows_job_handle=windows_job,
            root_pid=process.pid,
        )
    return process


def owns_job_process(job_id: str | UUID) -> bool:
    with _LOCK:
        return str(job_id) in _OWNED


def stop_owned_job(job_id: str | UUID) -> bool:
    """Stop only a process this module spawned."""
    key = str(job_id)
    with _LOCK:
        owned = _OWNED.get(key)
    if owned is None:
        return False
    process = owned.process
    if process.poll() is not None:
        _release_owned_job(key)
        return False
    if os.name == "nt":
        _close_windows_job(owned.windows_job_handle)
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
    return True


def _release_owned_job(job_id: str) -> None:
    with _LOCK:
        owned = _OWNED.pop(job_id, None)
    if owned is not None:
        _close_windows_job(owned.windows_job_handle)


def pop_owned_process(job_id: str | UUID) -> subprocess.Popen[str] | None:
    key = str(job_id)
    with _LOCK:
        owned = _OWNED.pop(key, None)
    if owned is None:
        return None
    _close_windows_job(owned.windows_job_handle)
    return owned.process


def runner_env_marker() -> str:
    contract = detect_isolation_contract()
    return f"aoitalk-isolated:{contract.backend}" if contract else "aoitalk-unavailable"


__all__ = [
    "AppJobIsolationError",
    "IsolationContract",
    "detect_isolation_contract",
    "owns_job_process",
    "pop_owned_process",
    "require_isolation_contract",
    "runner_env_marker",
    "spawn_isolated_process",
    "stop_owned_job",
]
