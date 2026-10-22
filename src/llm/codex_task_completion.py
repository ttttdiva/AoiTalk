"""Codex app-server inference with execution owned by the AoiTalk task runner.

The trusted coordinator keeps its existing Codex authentication. No environment
is attached to the model. Dynamic calls are returned to the caller; this module
never executes their arguments or implements filesystem/command tools.
"""
from __future__ import annotations

import ast
import json
import logging
import math
import os
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping


logger = logging.getLogger(__name__)


class CodexTaskProtocolError(RuntimeError):
    def __init__(self, code: str, *, detail: Mapping[str, Any] | None = None):
        self.code = str(code)
        self.detail = dict(detail) if isinstance(detail, Mapping) else detail
        message = self.code
        if self.detail is not None:
            message += ":" + json.dumps(self.detail, ensure_ascii=False, separators=(",", ":"))
        super().__init__(message)


MAX_MESSAGE_CHARS = 1024 * 1024
SUPPORTED_CODEX_VERSIONS = ("0.159.2", "0.160.0", "0.162.0")
_CODEX_VERSION_RE = re.compile(
    r"/(?:" + "|".join(re.escape(version) for version in SUPPORTED_CODEX_VERSIONS) + r")(?:\s|$)"
)
_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_DISABLED_FEATURES = (
    "apps", "code_mode", "code_mode_only", "context_management",
    "current_time_reminder", "deferred_executor", "enable_fanout", "goals",
    "hooks", "image_generation", "memories", "multi_agent", "multi_agent_v2",
    "plugins", "request_permissions_tool", "shell_snapshot", "shell_tool",
    "standalone_web_search", "token_budget", "tool_suggest", "unified_exec", "view_image", "sleep_tool",
)
_CODEX_ERROR_INFO_ENUMS = frozenset({
    "contextWindowExceeded", "sessionBudgetExceeded", "usageLimitExceeded",
    "rateLimitExceeded", "flexUnavailable", "serverOverloaded", "cyberPolicy",
    "misalignmentPolicyViolation", "tooManyDenials", "internalServerError",
    "unauthorized", "badRequest", "threadRollbackFailed", "sandboxError", "other",
})
_CODEX_ERROR_INFO_OBJECT_FIELDS = {
    "httpConnectionFailed": "httpStatusCode",
    "responseStreamConnectionFailed": "httpStatusCode",
    "responseStreamDisconnected": "httpStatusCode",
    "responseTooManyFailedAttempts": "httpStatusCode",
    "activeTurnNotSteerable": "turnKind",
}
_CODEX_ERROR_INFO_TURN_KINDS = frozenset({"review", "compact"})


def _redact_server_error_text(value: Any) -> str:
    """Drop provider-controlled free text from the durable diagnostic."""

    return "server reported an error"


def _project_codex_error_info(value: Any) -> Any:
    """Keep only the reviewed 0.162 CodexErrorInfo discriminant fields."""

    if isinstance(value, str):
        return value if value in _CODEX_ERROR_INFO_ENUMS else "unknown"
    if not isinstance(value, Mapping):
        return "unknown"
    for variant, field in _CODEX_ERROR_INFO_OBJECT_FIELDS.items():
        if variant not in value:
            continue
        raw_detail = value.get(variant)
        projected: dict[str, Any] = {}
        if isinstance(raw_detail, Mapping):
            raw_scalar = raw_detail.get(field)
            if field == "httpStatusCode":
                if isinstance(raw_scalar, int) and not isinstance(raw_scalar, bool) and 0 <= raw_scalar <= 65_535:
                    projected[field] = raw_scalar
            elif isinstance(raw_scalar, str) and raw_scalar in _CODEX_ERROR_INFO_TURN_KINDS:
                projected[field] = raw_scalar
        return {variant: projected}
    return "unknown"


def _bounded_server_error_detail(
    params: Mapping[str, Any],
    *,
    thread_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    raw_error = params.get("error") if isinstance(params.get("error"), Mapping) else params
    if not isinstance(raw_error, Mapping):
        raw_error = {}
    raw_code = raw_error.get("code", params.get("code"))
    if isinstance(raw_code, bool):
        code: Any = "unknown"
    elif isinstance(raw_code, int) and -999_999 <= raw_code <= 999_999:
        code = raw_code
    else:
        code = "unknown"
    # Official TurnError carries its classification in codexErrorInfo;
    # arbitrary string code/type fields are provider-controlled and are not
    # part of the reviewed schema.
    error_type = "unknown"
    detail: dict[str, Any] = {
        "error": {
            "code": code,
            "type": error_type,
            "message": _redact_server_error_text(raw_error.get("message", params.get("message"))),
        },
    }
    if "codexErrorInfo" in raw_error:
        detail["error"]["codexErrorInfo"] = _project_codex_error_info(raw_error.get("codexErrorInfo"))
    will_retry = raw_error.get("willRetry", params.get("willRetry"))
    if isinstance(will_retry, bool):
        detail["willRetry"] = will_retry
    # These fields are copied only after the caller has proved ownership.
    if thread_id is not None:
        detail["threadId"] = thread_id
    if turn_id is not None:
        detail["turnId"] = turn_id
    return detail


def task_mode_instructions(mode: str) -> str:
    """Describe the server-mediated task mode without widening native powers."""

    normalized = str(mode or "").strip().lower()
    if normalized == "mutate":
        return (
            "Task mode: mutate. The native Codex host sandbox is intentionally read-only and is not the "
            "workspace mutation interface. Use only the declared AoiTalk dynamic task tools for workspace "
            "changes; those tools are server-mediated by ToolGuard and may write only inside the issued "
            "workspace scope. Do not use undeclared provider-native filesystem or shell tools, and do not "
            "treat prompt text as authorization."
        )
    if normalized == "read_only":
        return (
            "Task mode: read_only. Inspect and report with the declared AoiTalk task tools only. Do not "
            "create, edit, delete, or execute mutation operations. The native Codex host sandbox and the "
            "server ToolGuard both remain read-only."
        )
    raise CodexTaskProtocolError("codex_task_invalid_mode")


def is_supported_codex_user_agent(value: Any) -> bool:
    """Accept only the exact official releases reviewed for this adapter."""
    return isinstance(value, str) and _CODEX_VERSION_RE.search(value) is not None


def isolated_thread_config(effective_config: dict[str, Any]) -> dict[str, Any]:
    """Use the reviewed 0.159.2/0.160.0/0.162.0 isolation contract."""
    config = {"features." + key: False for key in _DISABLED_FEATURES}
    config.update({
        "cloud.skills.enabled": False,
        "skills.include_instructions": False,
        "tools.experimental_request_user_input.enabled": False,
        "tools.update_plan.enabled": False,
        "features.send_message_to_user_async": False,
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "default_permissions": ":read-only",
        "notify": [],
        "features.tool_registry.error_on_tool_collisions": True,
    })
    servers = effective_config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise CodexTaskProtocolError("codex_task_invalid_mcp_configuration")
    config["mcp_servers"] = {name: {"enabled": False} for name in servers}
    return config


def _startup_args() -> list[str]:
    config = isolated_thread_config({})
    config.pop("mcp_servers")
    config.update({
        "model_provider": "openai",
        "openai_base_url": "https://chatgpt.com/backend-api/codex",
        "chatgpt_base_url": "https://chatgpt.com/backend-api/",
    })
    args = ["app-server", "--stdio"]
    for key, value in config.items():
        args += ["-c", key + "=" + json.dumps(value)]
    return args


def _npm_native_binary(package: Path, machine: str) -> Path:
    arch = {"AMD64": "x64", "x86_64": "x64", "ARM64": "arm64", "aarch64": "arm64"}.get(machine)
    triple = {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}.get(arch)
    if not triple:
        raise CodexTaskProtocolError("codex_task_unsupported_windows_architecture")
    name = "codex-win32-" + arch
    for candidate in (package / "node_modules" / "@openai" / name, package.parent / name):
        try:
            metadata = json.loads((candidate / "package.json").read_text(encoding="utf-8"))
            binary = candidate / "vendor" / triple / "bin" / "codex.exe"
            release = next(
                (version for version in SUPPORTED_CODEX_VERSIONS
                 if metadata.get("version") == version + "-win32-" + arch),
                None,
            )
            expected_version = str(release) + "-win32-" + arch
            official = (metadata.get("name") == "@openai/" + name
                        and release is not None)
            if metadata.get("name") == "@openai/codex":
                parent = json.loads((package / "package.json").read_text(encoding="utf-8"))
                version = expected_version
                official = (
                    parent.get("name") == "@openai/codex"
                    and release is not None
                    and parent.get("version") == release
                    and metadata.get("version") == version
                    and parent.get("optionalDependencies", {}).get("@openai/" + name)
                    == "npm:@openai/codex@" + version
                )
            if official and binary.is_file():
                return binary
        except (OSError, ValueError):
            continue
    raise CodexTaskProtocolError("codex_task_official_native_binary_not_found")


def codex_app_server_command() -> list[str]:
    """Resolve a trusted deployment executable without invoking a command shell."""
    configured = os.environ.get("CODEX_BIN", "codex").strip()
    executable = shutil.which(configured)
    if not executable:
        raise CodexTaskProtocolError("codex_task_cli_not_found")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # The official npm launcher is JavaScript. Do not run the Windows
        # wrapper through either a shell or a Linux execution environment.
        package = path.parent / "node_modules" / "@openai" / "codex"
        metadata = package / "package.json"
        try:
            official = json.loads(metadata.read_text(encoding="utf-8")).get("name") == "@openai/codex"
        except (OSError, ValueError):
            official = False
        if not official:
            raise CodexTaskProtocolError("codex_task_unsupported_windows_launcher")
        # Own the actual server PID so timeout/cancel cannot orphan a native
        # process behind an npm/Node wrapper.
        return [str(_npm_native_binary(package, platform.machine())), *_startup_args()]
    return [str(path), *_startup_args()]


_REMOTE_EXEC_KEYS = frozenset({
    "CODEX_EXEC_SERVER_URL",
    "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL",
    "CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID",
    "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN",
    "CODEX_EXEC_SERVER_NOISE_CHATGPT_ACCOUNT_ID",
})


def _windows_profile_directory() -> Path:
    # dirs 6.0 (used by pinned Codex) resolves Windows home via KnownFolder
    # Profile, not USERPROFILE/HOMEDRIVE. Do not redirect the child's auth home.
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    folder_id = GUID.from_buffer_copy(uuid.UUID("5E6C858F-0E22-4760-9AFE-EA3317B67173").bytes_le)
    shell, ole = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
    pointer = ctypes.c_wchar_p()
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                         wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer))
    if result != 0 or not pointer.value:
        raise OSError("KnownFolder Profile unavailable")
    try:
        return Path(pointer.value)
    finally:
        ole.CoTaskMemFree(ctypes.cast(pointer, ctypes.c_void_p))


def _default_codex_home(environment: Mapping[str, str], platform_name: str | None = None) -> Path:
    if (platform_name or os.name) == "nt":
        return _windows_profile_directory() / ".codex"
    # dirs-sys ignores an empty HOME and falls back to the passwd record.
    value = environment.get("HOME")
    if not value:
        import pwd
        value = pwd.getpwuid(os.getuid()).pw_dir
    if not value:
        raise OSError("default home unavailable")
    return Path(value) / ".codex"


def isolated_process_environment() -> dict[str, str]:
    """Check registration metadata, then restrict only this owned child.

    Both supported releases prioritize environments.toml over EXEC_SERVER_URL=none.
    Any present or unreadable registry is unsupported; its contents are never
    read and it is never rewritten. Concurrent changes to Codex home by other
    host processes are outside this deployment precondition.
    """
    environment = os.environ.copy()
    try:
        if "CODEX_HOME" in environment and not environment["CODEX_HOME"]:
            raise CodexTaskProtocolError("codex_task_home_metadata_unverified")
        home = Path(environment["CODEX_HOME"]) if "CODEX_HOME" in environment else _default_codex_home(environment)
    except (OSError, RuntimeError, KeyError) as exc:
        raise CodexTaskProtocolError("codex_task_home_metadata_unverified") from exc
    if not home.is_absolute():
        raise CodexTaskProtocolError("codex_task_home_metadata_unverified")
    try:
        info = home.lstat()
        if (not stat.S_ISDIR(info.st_mode) or home.is_symlink()
                or int(getattr(info, "st_file_attributes", 0)) & 0x400
                or home.resolve(strict=True) != home):
            raise CodexTaskProtocolError("codex_task_home_metadata_unverified")
        try:
            (home / "environments.toml").lstat()
        except FileNotFoundError:
            pass
        else:
            raise CodexTaskProtocolError("codex_task_environment_registry_configured")
    except CodexTaskProtocolError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CodexTaskProtocolError("codex_task_environment_registry_unverified") from exc
    for key in list(environment):
        if key.upper() in _REMOTE_EXEC_KEYS:
            environment.pop(key)
    environment["CODEX_EXEC_SERVER_URL"] = "none"
    environment["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] = "1"
    return environment


class _ProcessTransport:
    def __init__(self, command: list[str], cwd: Path, *, raw_bytes: bool = False, profile=None):
        self._raw_bytes = raw_bytes
        environment = profile.environment() if profile is not None else isolated_process_environment()
        self.process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004) if os.name == "nt" else 0,
            env=environment,
        )
        self._job = None
        if os.name == "nt":
            try:
                self._job = _WindowsProcessJob(self.process)
                self._job.resume(self.process.pid)
            except Exception:
                if self._job is not None:
                    self._job.close()
                self.process.kill()
                self.process.wait(timeout=5)
                raise
        self.messages: queue.Queue[Any] = queue.Queue(maxsize=32)
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._writer_active = False
        self._writes: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._control: Callable[[], Any] = lambda: None
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        if profile is not None:
            try:
                self._release_profile = profile.own_cleanup(self.close)
            except Exception:
                self.close()
                raise

    def set_control(self, callback: Callable[[], Any]) -> None:
        self._control = callback

    def _write_loop(self) -> None:
        while not self._closed.is_set():
            try:
                data, done, outcome = self._writes.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._state_lock:
                if self._closed.is_set():
                    outcome["error"] = CodexTaskProtocolError("codex_task_connection_closed")
                    done.set()
                    return
                self._writer_active = True
            try:
                view = memoryview(data)
                while view:
                    written = os.write(self.process.stdin.fileno(), view[:65536])
                    if written <= 0:
                        raise BrokenPipeError()
                    view = view[written:]
            except (OSError, ValueError) as exc:
                outcome["error"] = exc
            finally:
                with self._state_lock:
                    self._writer_active = False
                done.set()

    def _put(self, value: Any) -> None:
        while not self._closed.is_set():
            try:
                self.messages.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _read_stdout(self) -> None:
        try:
            while not self._closed.is_set():
                if self._raw_bytes:
                    chunk = os.read(self.process.stdout.fileno(), 4096)
                    if not chunk:
                        self._put(CodexTaskProtocolError("codex_task_connection_closed"))
                        return
                    self._put(chunk)
                    continue
                line = self.process.stdout.readline(MAX_MESSAGE_CHARS + 1)
                if not line:
                    self._put(CodexTaskProtocolError("codex_task_connection_closed"))
                    return
                if len(line) > MAX_MESSAGE_CHARS:
                    raise CodexTaskProtocolError("codex_task_response_too_large")
                message = json.loads(line.decode("utf-8"), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
                if not isinstance(message, dict):
                    raise CodexTaskProtocolError("codex_task_invalid_response")
                self._put(message)
        except Exception:
            self._put(CodexTaskProtocolError("codex_task_invalid_protocol_stream"))

    def _drain_stderr(self) -> None:
        # Native diagnostics can contain operator paths/configuration. Drain
        # them to avoid blocking; never copy them into a model response.
        try:
            while self.process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def send(self, value: dict[str, Any]) -> None:
        if self._raw_bytes:
            raise CodexTaskProtocolError("codex_task_raw_transport_requires_bytes")
        encoded = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.send_bytes(encoded)

    def send_bytes(self, encoded: bytes) -> None:
        if not isinstance(encoded, bytes) or len(encoded) > MAX_MESSAGE_CHARS:
            raise CodexTaskProtocolError("codex_task_request_too_large")
        done, outcome = threading.Event(), {}
        while True:
            self._control()
            if self._closed.is_set():
                raise CodexTaskProtocolError("codex_task_connection_closed")
            try:
                self._writes.put((encoded, done, outcome), timeout=0.05)
                break
            except queue.Full:
                continue
        while not done.wait(0.05):
            self._control()
            if self._closed.is_set():
                raise CodexTaskProtocolError("codex_task_connection_closed")
        self._control()
        if "error" in outcome:
            raise CodexTaskProtocolError("codex_task_pipe_write_failed") from outcome["error"]

    def receive(self, timeout: float) -> Any:
        value = self.messages.get(timeout=timeout)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            busy = self._writer_active
        release = getattr(self, "_release_profile", None)
        if release is not None:
            release()
        # Never close a buffered stream under a blocked writer. The streams
        # are unbuffered; kill the owned tree first when an OS write is pending.
        if not busy:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._terminate_tree()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self._writer.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _terminate_tree(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()


class _WindowsProcessJob:
    """An owned, temporary kill-on-close Job Object; no system policy changes."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "codex_task_job_create_failed")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "codex_task_job_assignment_failed")

    def resume(self, pid: int) -> None:
        """Resume only this suspended process after Job membership is fixed."""
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]

        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.kernel.Thread32First.restype = wintypes.BOOL
        self.kernel.Thread32Next.argtypes = self.kernel.Thread32First.argtypes
        self.kernel.Thread32Next.restype = wintypes.BOOL
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "codex_task_thread_snapshot_failed")
        resumed = 0
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.owner_pid == pid:
                    handle = self.kernel.OpenThread(0x0002, False, entry.thread_id)
                    if not handle:
                        raise OSError(ctypes.get_last_error(), "codex_task_thread_open_failed")
                    try:
                        previous = self.kernel.ResumeThread(handle)
                        if previous == 0xFFFFFFFF:
                            raise OSError(ctypes.get_last_error(), "codex_task_thread_resume_failed")
                        if previous > 0:
                            resumed += 1
                    finally:
                        self.kernel.CloseHandle(handle)
                more = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if not resumed:
            raise CodexTaskProtocolError("codex_task_suspended_thread_not_found")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class CodexTaskCompletionClient:
    """Stateful one-completion adapter for the existing guarded task tool loop."""

    def __init__(
        self, *, model: str, effort: str, workspace: str | Path,
        timeout_seconds: float, cancelled: Callable[[], bool],
        task_mode: str = "read_only",
        checkpoint: Callable[[], Any] | None = None,
        instruction_review: Any | None = None,
        profile: Any | None = None,
        command: list[str] | None = None,
        transport_factory: Callable[..., Any] = _ProcessTransport,
        user_id: str | None = None,
        project_id: Any = None,
        session_id: Any = None,
        usage_recorder: Callable[[Mapping[str, Any]], Any] | None = None,
    ):
        if not model or not effort:
            raise CodexTaskProtocolError("codex_task_requires_explicit_model_and_effort")
        self.task_mode = str(task_mode or "").strip().lower()
        self._task_mode_instructions = task_mode_instructions(self.task_mode)
        if profile is not None and self.task_mode != "read_only":
            raise CodexTaskProtocolError("codex_task_app_profile_mutation_unsupported")
        raw = Path(workspace)
        if not raw.is_absolute() or not raw.is_dir():
            raise CodexTaskProtocolError("codex_task_requires_absolute_workspace")
        self._workspace_input = raw
        self._workspace = raw.resolve(strict=True)
        self.model_name, self.effort, self.base_url = model, effort, None
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise CodexTaskProtocolError("codex_task_invalid_timeout")
        self._deadline = time.monotonic() + timeout_seconds
        info = self._workspace.stat()
        self._workspace_identity = (info.st_dev, info.st_ino)
        self._cancelled, self._checkpoint_callback = cancelled, checkpoint
        self._instruction_review = instruction_review
        self._profile = profile
        if profile is not None:
            from ..services.codex_app_profiles import CodexProfileLease
            if not isinstance(profile, CodexProfileLease) or instruction_review is not None:
                raise CodexTaskProtocolError("codex_task_invalid_app_profile")
            profile.check(owner=str(user_id), read_roots=[self._workspace])
        self._command, self._transport_factory = command, transport_factory
        # TaskContext attribution is optional so the guarded client remains
        # backwards compatible with callers that do not have a user/session.
        # ``persist_usage_sync`` reads these fields through the small adapter
        # below when no test/integration recorder is supplied.
        self._usage_user_id = str(user_id) if user_id is not None else None
        self.current_project_id = project_id
        self.current_session_id = session_id
        self._usage_recorder = usage_recorder
        self._transport = None
        self._request_id = 0
        self._thread_id = self._turn_id = None
        self._turn_requested = False
        self._event_turn_id = None
        self._tool_catalog: dict[str, dict[str, Any]] = {}
        self._queued_calls: deque[dict[str, Any]] = deque()
        self._awaiting: dict[str, dict[str, Any]] = {}
        self._tool_calls_seen: set[str] = set()
        self._final_text: str | None = None
        self._finished = False
        self._tools_enabled = True
        self._closed = False
        self._release_profile = None
        self._owned_channel = None
        self._account_snapshot = None
        self._account_dirty = False
        # Both supported releases report ``last`` for one provider response and ``total``
        # cumulatively for the ephemeral thread.  Keep every distinct
        # cumulative snapshot pending until the turn finishes (or is
        # cancelled), then persist each provider-reported ``last`` once.
        self._usage_events: dict[str, list[tuple[tuple[int, ...], dict[str, Any]]]] = {}
        self._usage_totals_seen: set[tuple[str, tuple[int, ...]]] = set()
        self._usage_totals_last: tuple[int, ...] | None = None
        self._usage_recorded: set[tuple[str, tuple[int, ...]]] = set()

    @staticmethod
    def _message_content(message: Any) -> str:
        return str(getattr(message, "content", "") or "")

    def describe(self) -> dict[str, Any]:
        return {"provider": "codex-cli", "model": self.model_name, "effort": self.effort,
                "transport": "app-server-stdio", "native_environment": False}

    def build_request(self, messages, tools):
        request = self._build_api_kwargs(messages, None, None)
        if tools:
            request.update(tools=tools, tool_choice="auto")
        return request

    def complete(self, request):
        return self._tool_loop_completion(request)

    def message_content(self, message):
        return self._message_content(message)

    def _get_session_user_id(self) -> str | None:
        """Expose optional TaskContext attribution to ``persist_usage_sync``."""

        return self._usage_user_id

    @staticmethod
    def _usage_int(value: Any, field: str, *, default: int | None = None) -> int:
        if value is None and default is not None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodexTaskProtocolError("codex_task_invalid_token_usage:" + field)
        return value

    @classmethod
    def _codex_usage_breakdown(cls, value: Any, *, label: str) -> tuple[tuple[int, ...], dict[str, Any]]:
        """Validate and normalize one supported official token usage object.

        The app-server schema uses camelCase and distinguishes ``last`` from
        ``total``.  Do not accept estimates or silently fill required fields:
        only provider-reported counters enter the usage ledger.
        """

        if not isinstance(value, dict):
            raise CodexTaskProtocolError("codex_task_invalid_token_usage:" + label)
        fields = (
            "inputTokens", "outputTokens", "cachedInputTokens",
            "reasoningOutputTokens", "totalTokens",
        )
        values = tuple(cls._usage_int(value.get(field), label + "." + field) for field in fields)
        cache_write = cls._usage_int(
            value.get("cacheWriteInputTokens"),
            label + ".cacheWriteInputTokens",
            default=0,
        )
        total = {
            "input_tokens": values[0],
            "output_tokens": values[1],
            "total_tokens": values[4],
            "cached_tokens": values[2],
            "cache_read_tokens": values[2],
            "cache_write_tokens": cache_write,
            "reasoning_tokens": values[3],
            "metrics_source": "provider_reported:codex_app_server",
        }
        return values + (cache_write,), total

    def _handle_token_usage_update(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        if (
            not self._thread_id
            or thread_id != self._thread_id
            or not isinstance(turn_id, str)
            or not turn_id
            or (self._turn_id and turn_id != self._turn_id)
            or (self._event_turn_id and turn_id != self._event_turn_id)
        ):
            raise CodexTaskProtocolError("codex_task_mismatched_token_usage_owner")
        self._event_turn_id = turn_id
        token_usage = params.get("tokenUsage")
        if not isinstance(token_usage, dict):
            raise CodexTaskProtocolError("codex_task_invalid_token_usage:tokenUsage")
        total_key, _ = self._codex_usage_breakdown(token_usage.get("total"), label="total")
        _, last_usage = self._codex_usage_breakdown(token_usage.get("last"), label="last")
        if self._usage_totals_last is not None and any(
            current < previous
            for current, previous in zip(total_key, self._usage_totals_last)
        ):
            raise CodexTaskProtocolError("codex_task_non_monotonic_token_usage")
        self._usage_totals_last = total_key
        event_key = (turn_id, total_key)
        if event_key in self._usage_totals_seen:
            return
        self._usage_totals_seen.add(event_key)
        last_usage["turn_id"] = turn_id
        self._usage_events.setdefault(turn_id, []).append((total_key, last_usage))

    def _persist_usage_event(self, usage: Mapping[str, Any]) -> None:
        payload = dict(usage)
        payload.setdefault("provider", "codex-cli")
        payload.setdefault("model", self.model_name)
        payload.setdefault("requested_model", self.model_name)
        payload.setdefault("resolved_model", self.model_name)
        payload.setdefault("user_id", self._usage_user_id)
        payload.setdefault("project_id", self.current_project_id)
        payload.setdefault("session_id", self.current_session_id)
        payload.setdefault("request_type", "codex_task")
        recorder = self._usage_recorder
        try:
            if recorder is not None:
                recorder(payload)
                return
            from .conversation_context import persist_usage_sync

            persist_usage_sync(
                self,
                provider="codex-cli",
                model=self.model_name,
                usage=payload,
                requested_model=self.model_name,
                resolved_model=self.model_name,
                request_type="codex_task",
            )
        except Exception:
            # Accounting is a side effect.  A DB readiness/migration failure
            # must never turn a valid provider completion into a task failure.
            logger.debug("Codex task usage persistence failed", exc_info=True)

    def _flush_usage_for_turn(self, turn_id: str | None) -> None:
        if not isinstance(turn_id, str) or not turn_id:
            return
        for total_key, usage in self._usage_events.pop(turn_id, []):
            event_key = (turn_id, total_key)
            if event_key in self._usage_recorded:
                continue
            self._usage_recorded.add(event_key)
            self._persist_usage_event(usage)

    def close(self):
        self.cleanup()

    def _build_api_kwargs(self, messages, temperature, max_tokens, **_kwargs):
        return {"model": self.model_name, "messages": messages}

    def _check(self) -> None:
        if self._closed:
            raise CodexTaskProtocolError("codex_task_client_closed")
        # The owning task maps cancellation/deadline to its public task state.
        if self._checkpoint_callback:
            self._checkpoint_callback()
        if self._instruction_review is not None:
            self._instruction_review.check()
        if self._profile is not None:
            self._profile.check(owner=self._usage_user_id, read_roots=[self._workspace])
        if self._cancelled():
            # Cancellation may arrive during the metadata/lease checks above.
            # Let the owning runner classify that late cancellation too.
            if self._checkpoint_callback:
                self._checkpoint_callback()
            raise CodexTaskProtocolError("codex_task_cancelled")
        if time.monotonic() >= self._deadline:
            raise TimeoutError("codex_task_timeout")
        if self._workspace_input.resolve(strict=True) != self._workspace:
            raise CodexTaskProtocolError("codex_task_workspace_changed")
        info = self._workspace.stat()
        if (info.st_dev, info.st_ino) != self._workspace_identity:
            raise CodexTaskProtocolError("codex_task_workspace_replaced")

    def _receive(self) -> dict[str, Any]:
        while True:
            self._check()
            try:
                return self._transport.receive(timeout=min(0.05, max(0.001, self._deadline-time.monotonic())))
            except queue.Empty:
                continue

    def _send(self, value: dict[str, Any]) -> None:
        self._check()
        self._transport.send(value)

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = "aoitalk-" + str(self._request_id)
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    code = (message.get("error") or {}).get("code")
                    raise CodexTaskProtocolError(f"codex_task_rpc_failed:{method}:{code}")
                if "result" not in message:
                    raise CodexTaskProtocolError("codex_task_missing_rpc_result")
                return message["result"]
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, Mapping) else {}
        if method == "account/updated":
            self._account_dirty = True
            return
        if method == "thread/tokenUsage/updated":
            self._handle_token_usage_update(params)
            return
        if "id" in message and method:
            if method != "item/tool/call":
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "Only declared AoiTalk task tools are available",
                }})
                raise CodexTaskProtocolError("codex_task_unexpected_server_request")
            event_turn = params.get("turnId")
            if not self._thread_id or not self._turn_requested or params.get("threadId") != self._thread_id or (
                not isinstance(event_turn, str) or not event_turn or
                (self._turn_id and event_turn != self._turn_id) or
                (self._event_turn_id and event_turn != self._event_turn_id)
            ):
                raise CodexTaskProtocolError("codex_task_mismatched_tool_owner")
            self._event_turn_id = event_turn
            if not self._tools_enabled:
                raise CodexTaskProtocolError("codex_task_tool_calls_when_disabled")
            name, arguments = params.get("tool"), params.get("arguments")
            call_id = params.get("callId")
            if params.get("namespace") not in (None, "") or name not in self._tool_catalog:
                raise CodexTaskProtocolError("codex_task_undeclared_tool")
            if not isinstance(arguments, dict) or not isinstance(call_id, str) or not call_id:
                raise CodexTaskProtocolError("codex_task_invalid_tool_call")
            if call_id in self._tool_calls_seen:
                raise CodexTaskProtocolError("codex_task_duplicate_call_id")
            encoded = json.dumps(arguments, allow_nan=False)
            if len(encoded) > MAX_MESSAGE_CHARS:
                raise CodexTaskProtocolError("codex_task_tool_arguments_too_large")
            self._tool_calls_seen.add(call_id)
            self._queued_calls.append({
                "rpc_id": message["id"], "call_id": call_id,
                "name": name, "arguments": encoded,
            })
            return
        if method != "error" and self._thread_id and params.get("threadId") not in (None, self._thread_id):
            raise CodexTaskProtocolError("codex_task_mismatched_notification_owner")
        if method in {"item/started", "item/completed"}:
            event_turn = params.get("turnId")
            if not self._thread_id or params.get("threadId") != self._thread_id or not self._turn_requested or not isinstance(event_turn, str) or not event_turn or (
                self._turn_id and event_turn != self._turn_id
            ) or (self._event_turn_id and event_turn != self._event_turn_id):
                raise CodexTaskProtocolError("codex_task_mismatched_item_owner")
            self._event_turn_id = event_turn
            item = params.get("item") or {}
            kind = item.get("type")
            if kind not in {"userMessage", "agentMessage", "reasoning", "dynamicToolCall"}:
                raise CodexTaskProtocolError("codex_task_unexpected_native_item")
            if kind == "agentMessage" and method == "item/completed" and item.get("phase") in (None, "final_answer"):
                self._final_text = str(item.get("text") or "")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            event_turn = turn.get("id")
            if params.get("threadId") != self._thread_id or not self._turn_requested or not isinstance(event_turn, str) or not event_turn or (
                self._turn_id and event_turn != self._turn_id
            ) or (self._event_turn_id and event_turn != self._event_turn_id):
                raise CodexTaskProtocolError("codex_task_mismatched_turn")
            self._event_turn_id = event_turn
            if turn.get("status") != "completed":
                raise CodexTaskProtocolError("codex_task_turn_failed:" + str(turn.get("status")))
            if self._awaiting or self._queued_calls:
                raise CodexTaskProtocolError("codex_task_completed_with_pending_tools")
            for item in turn.get("items") or []:
                if not isinstance(item, dict) or item.get("type") not in {"userMessage", "agentMessage", "reasoning", "dynamicToolCall"}:
                    raise CodexTaskProtocolError("codex_task_unexpected_native_item")
                if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                    self._final_text = str(item.get("text") or "")
            if not self._final_text:
                raise CodexTaskProtocolError("codex_task_empty_final_answer")
            self._flush_usage_for_turn(event_turn)
            self._finished = True
        elif method == "error":
            supplied_thread = params.get("threadId")
            supplied_turn = params.get("turnId")
            expected_turns = {
                value for value in (self._turn_id, self._event_turn_id)
                if isinstance(value, str) and value
            }
            if supplied_thread is not None and (
                not isinstance(supplied_thread, str)
                or not supplied_thread
                or not self._thread_id
                or supplied_thread != self._thread_id
            ):
                raise CodexTaskProtocolError("codex_task_mismatched_server_error_owner")
            if supplied_turn is not None and (
                not isinstance(supplied_turn, str)
                or not supplied_turn
                or not expected_turns
                or supplied_turn not in expected_turns
            ):
                raise CodexTaskProtocolError("codex_task_mismatched_server_error_owner")
            owned_thread = self._thread_id if supplied_thread == self._thread_id else None
            owned_turn = supplied_turn if supplied_turn in expected_turns else None
            detail = _bounded_server_error_detail(
                params,
                thread_id=owned_thread,
                turn_id=owned_turn,
            )
            raise CodexTaskProtocolError("codex_task_server_error", detail=detail)

    def _start(self, request: dict[str, Any]) -> None:
        functions = []
        for tool in request.get("tools") or []:
            function = tool.get("function") if isinstance(tool, dict) and tool.get("type") == "function" else None
            if not isinstance(function, dict) or not _NAME.fullmatch(str(function.get("name", ""))):
                raise CodexTaskProtocolError("codex_task_invalid_tool_schema")
            name = function["name"]
            if name in self._tool_catalog:
                raise CodexTaskProtocolError("codex_task_duplicate_tool_name")
            schema = function.get("parameters")
            if not isinstance(schema, dict):
                raise CodexTaskProtocolError("codex_task_missing_tool_parameters")
            self._tool_catalog[name] = tool
            functions.append({"type": "function", "name": name,
                              "description": str(function.get("description") or ""), "inputSchema": schema})
        self._check()
        reuse_owned = self._profile is not None and self._profile.session is not None
        if reuse_owned:
            self._owned_channel = self._profile.session.acquire_task(self._profile, self._check, self._workspace)
            self._transport = self._owned_channel
            initialized = self._profile.session.initialized
        else:
            command = self._command or codex_app_server_command()
            if self._profile is not None:
                command = self._profile.command(command)
            if self._instruction_review is not None:
                self._instruction_review.verify_command(command)
            self._transport = self._transport_factory(command, self._workspace, profile=self._profile) if self._profile is not None else self._transport_factory(command, self._workspace)
        if self._profile is not None:
            self._release_profile = self._profile.own_cleanup(self.close)
        set_control = getattr(self._transport, "set_control", None)
        if callable(set_control):
            set_control(self._check)
        if not reuse_owned:
            initialized = self._rpc("initialize", {
                "clientInfo": {"name": "aoitalk_operations", "title": "AoiTalk Operations", "version": "1"},
                "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True},
            })
        if not is_supported_codex_user_agent(initialized.get("userAgent")):
            raise CodexTaskProtocolError("codex_task_unverified_cli_version")
        if self._profile is not None and initialized.get("codexHome") != str(self._profile.home):
            raise CodexTaskProtocolError("codex_task_app_profile_home_mismatch")
        if self._instruction_review is not None:
            self._instruction_review.verify_cli(command, initialized["userAgent"])
        if not reuse_owned:
            self._send({"method": "initialized"})
        effective = self._rpc("config/read", {"includeLayers": False, "cwd": str(self._workspace)})
        if not isinstance(effective.get("config"), dict):
            raise CodexTaskProtocolError("codex_task_missing_effective_configuration")
        effective_config = effective["config"]
        for key, permitted in {
            "openai_base_url": {None, "https://chatgpt.com/backend-api/codex"},
            "chatgpt_base_url": {None, "https://chatgpt.com/backend-api/"},
        }.items():
            if effective_config.get(key) not in permitted:
                raise CodexTaskProtocolError("codex_task_unapproved_provider_endpoint")
        custom_openai = (effective_config.get("model_providers") or {}).get("openai")
        if custom_openai:
            raise CodexTaskProtocolError("codex_task_custom_openai_provider_unsupported")
        if effective_config.get("model_instructions_file") or effective_config.get("instructions"):
            raise CodexTaskProtocolError("codex_task_unreviewed_host_instructions")
        self._account_snapshot = self._read_account_snapshot()
        if self._profile is not None:
            if effective_config.get("cli_auth_credentials_store") != "keyring":
                raise CodexTaskProtocolError("codex_task_app_profile_store_unverified")
            if self._account_snapshot != self._profile.account:
                raise CodexTaskProtocolError("codex_task_app_profile_account_mismatch")
        catalog = self._rpc("model/list", {"includeHidden": False})
        matching = [m for m in catalog.get("data", []) if m.get("model") == self.model_name]
        if len(matching) != 1 or self.effort not in {
            option.get("reasoningEffort") for option in matching[0].get("supportedReasoningEfforts", [])
        }:
            raise CodexTaskProtocolError("codex_task_unsupported_model_or_effort")
        config = isolated_thread_config(effective_config)
        messages = request.get("messages") or []
        instructions = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") in {"system", "developer"})
        started = self._rpc("thread/start", {
            "model": self.model_name, "modelProvider": "openai", "allowProviderModelFallback": False,
            "cwd": str(self._workspace), "ephemeral": True, "environments": [],
            "runtimeWorkspaceRoots": [], "selectedCapabilityRoots": [],
            "sandbox": "read-only", "approvalPolicy": "never", "config": config,
            "baseInstructions": (
                "You are the AoiTalk task model. Use only the declared task tools and follow the supplied "
                "task instructions. " + self._task_mode_instructions
            ),
            "developerInstructions": instructions, "dynamicTools": functions,
        })
        thread = started.get("thread") or {}
        if started.get("model") != self.model_name or started.get("modelProvider") != "openai":
            raise CodexTaskProtocolError("codex_task_model_route_mismatch")
        if thread.get("environments") != [] or (started.get("sandbox") or {}).get("type") != "readOnly":
            raise CodexTaskProtocolError("codex_task_native_environment_not_disabled")
        if self._instruction_review is not None:
            self._instruction_review.verify_sources(started.get("instructionSources"))
        elif started.get("instructionSources") != []:
            raise CodexTaskProtocolError("codex_task_unreviewed_host_instructions")
        self._thread_id = thread.get("id")
        if not isinstance(self._thread_id, str) or not self._thread_id:
            raise CodexTaskProtocolError("codex_task_missing_thread_id")
        if self._read_account_snapshot() != self._account_snapshot:
            raise CodexTaskProtocolError("codex_task_account_changed")
        user_messages = [m for m in messages if m.get("role") not in {"system", "developer"}]
        self._turn_requested = True
        turn = self._rpc("turn/start", {
            "threadId": self._thread_id, "model": self.model_name, "effort": self.effort,
            "environments": [], "runtimeWorkspaceRoots": [],
            "input": [{"type": "text", "text": json.dumps(user_messages, ensure_ascii=False)}],
        })
        self._turn_id = (turn.get("turn") or {}).get("id")
        if not isinstance(self._turn_id, str) or not self._turn_id:
            raise CodexTaskProtocolError("codex_task_missing_turn_id")
        if self._event_turn_id and self._event_turn_id != self._turn_id:
            raise CodexTaskProtocolError("codex_task_mismatched_turn")
        if self._owned_channel is not None:
            self._owned_channel.verify_model_contract()

    def _read_account_snapshot(self):
        value = self._rpc("account/read", {"refreshToken": False})
        account, routing = value.get("account") or {}, value.get("workspaceRouting") or {}
        if account.get("type") != "chatgpt" or not routing.get("chatgptAccountId"):
            raise CodexTaskProtocolError("codex_task_account_routing_unavailable")
        if routing.get("backendOrigin") != "https://chatgpt.com" or routing.get("accountRoutingOverride") != "NO_CONSTRAINT":
            raise CodexTaskProtocolError("codex_task_unapproved_account_routing")
        email = account.get("email")
        if not isinstance(email, str) or not email.strip():
            raise CodexTaskProtocolError("codex_task_account_identity_unavailable")
        # This snapshots the existing signed-in workspace; it does not claim
        # to identify an expected human or create an authentication grant.
        # Both supported account/read contracts expose email, not a stable user/session
        # identifier. Do not claim stronger identity proof than this API gives.
        return (email, routing["chatgptAccountId"], routing["backendOrigin"])

    def _verify_account(self):
        if self._read_account_snapshot() != self._account_snapshot:
            raise CodexTaskProtocolError("codex_task_account_changed")
        self._account_dirty = False

    def _resume(self, request: dict[str, Any]) -> None:
        self._verify_account()
        current = {t.get("function", {}).get("name"): t for t in request.get("tools") or []}
        if current and current != self._tool_catalog:
            raise CodexTaskProtocolError("codex_task_tool_catalog_changed")
        results = {}
        for message in request.get("messages") or []:
            if message.get("role") == "tool" and message.get("tool_call_id") in self._awaiting:
                key = message["tool_call_id"]
                if key in results:
                    raise CodexTaskProtocolError("codex_task_duplicate_tool_result")
                results[key] = message
        if set(results) != set(self._awaiting):
            raise CodexTaskProtocolError("codex_task_missing_guarded_tool_result")
        for call_id, pending in list(self._awaiting.items()):
            message = results[call_id]
            content = message.get("content")
            if not isinstance(content, str):
                raise CodexTaskProtocolError("codex_task_invalid_tool_result")
            # RegistryToolRouter reports policy rejections as Error: text.
            success = not bool(message.get("is_error")) and not content.lstrip().startswith("Error:")
            try:
                payload = json.loads(content)
            except ValueError:
                try:
                    payload = ast.literal_eval(content)
                except (ValueError, SyntaxError, RecursionError):
                    payload = None
            if isinstance(payload, dict) and payload.get("success") is False:
                success = False
            self._send({"id": pending["rpc_id"], "result": {
                "contentItems": [{"type": "inputText", "text": content}],
                "success": success,
            }})
            del self._awaiting[call_id]

    def _tool_loop_completion(self, request: dict[str, Any]) -> Any:
        try:
            self._check()
            if request.get("model") != self.model_name:
                raise CodexTaskProtocolError("codex_task_request_model_mismatch")
            if self._finished:
                raise CodexTaskProtocolError("codex_task_already_completed")
            self._tools_enabled = bool(request.get("tools")) and request.get("tool_choice") != "none"
            if self._transport is None:
                self._start(request)
            else:
                self._resume(request)
            while not self._queued_calls and not self._finished:
                self._handle(self._receive())
            if self._queued_calls:
                if not self._tools_enabled:
                    raise CodexTaskProtocolError("codex_task_tool_calls_when_disabled")
                if self._account_dirty:
                    self._verify_account()
                call = self._queued_calls.popleft()
                self._awaiting[call["call_id"]] = call
                message = SimpleNamespace(role="assistant", content="", tool_calls=[SimpleNamespace(
                    id=call["call_id"], type="function",
                    function=SimpleNamespace(name=call["name"], arguments=call["arguments"]),
                )])
                return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])
            self._verify_account()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(role="assistant", content=self._final_text, tool_calls=None),
                finish_reason="stop",
            )])
        except Exception:
            self.cleanup()
            raise

    def cleanup(self) -> None:
        if self._closed:
            return
        # Closing the owned stdio connection cancels its ephemeral thread. No
        # daemon, shared server, credential or global configuration is changed.
        # Do not use _rpc here: normal request checkpoints deliberately reject
        # cancellation and expired budgets. Cleanup must still stop this turn.
        transport = self._transport
        if self._transport is not None and self._thread_id:
            try:
                cleanup_deadline = time.monotonic() + 2
                def cleanup_check():
                    if time.monotonic() >= cleanup_deadline:
                        raise TimeoutError("codex_task_cleanup_timeout")
                set_control = getattr(self._transport, "set_control", None)
                if callable(set_control):
                    set_control(cleanup_check)
                if self._turn_id and not self._finished:
                    self._transport.send({"id": "aoitalk-stop", "method": "turn/interrupt", "params": {
                        "threadId": self._thread_id, "turnId": self._turn_id,
                    }})
                if self._owned_channel is None:
                    self._transport.send({"id": "aoitalk-detach", "method": "thread/unsubscribe", "params": {
                        "threadId": self._thread_id,
                    }})
            except Exception:
                pass
        self._closed = True
        if self._release_profile is not None:
            self._release_profile()
            self._release_profile = None
        try:
            if transport is not None:
                if self._owned_channel is not None:
                    self._owned_channel.finish(completed=self._finished, handler=self._handle)
                else:
                    transport.close()
        except Exception:
            # Transport teardown is best effort; usage accounting below must
            # still run without turning cancellation into a new task failure.
            pass
        finally:
            # A cancellation/deadline can happen after the provider has
            # reported usage but before ``turn/completed``.  Flush only
            # snapshots already observed; no estimate is fabricated for an
            # unseen cancellation.  This intentionally runs after the owned
            # process is closed so a slow recorder cannot orphan it.
            self._flush_usage_for_turn(self._turn_id)
