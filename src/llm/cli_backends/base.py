"""
Abstract base class for CLI-based LLM backends

Provides common interface for Antigravity CLI, Claude Code, Codex CLI, etc.
"""

import logging
import locale
import os
import base64
import queue
import shlex
import signal
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable, Mapping

from ..generation_cancellation import (
    GenerationCancelled,
    GenerationInterrupted,
    get_current_generation_cancellation,
    raise_if_generation_interrupted,
    current_cli_lease_receipt,
    mark_cli_backend_covered,
)
from ...utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)

# コマンドライン引数の長さ上限（安全マージン込み）
# Windows: 約32,767文字だが余裕を持たせる
_MAX_ARG_LENGTH = 8000
_MAX_INTERNAL_CLI_TEXT = 128 * 1024
_WINDOWS_USER_STATE_ENV_NAMES = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
_SCOPED_PROVIDER_ENV_NAMES = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "AOITALK_AGENT_RUN_ID",
        "AOITALK_AGENT_WORK_ITEM_ID",
        "AOITALK_AGENT_SESSION_ID",
        "AOITALK_AGENT_ID",
        "AOITALK_AGENT_REVISION_ID",
        "AOITALK_AGENT_BINDING_ID",
        "AOITALK_AGENT_BINDING_SNAPSHOT_HASH",
        "AOITALK_AGENT_CAPABILITY_MANIFEST_DIGEST",
        "AOITALK_AGENT_AUTHORITY_DIGEST",
        "AOITALK_AGENT_LEASE_OWNER",
        "AOITALK_AGENT_LEASE_TOKEN",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
)
_SCOPED_PROVIDER_ENV_MARKER = "SCOPED_ENV_V1"
CLIEventCallback = Callable[[str, Dict[str, Any]], Any]


@dataclass(frozen=True)
class CLIInvocationOptions:
    """Per-invocation controls for the isolated development lane.

    The legacy backend constructors and environment variables remain the
    source of defaults when this object is omitted.  Supplying an instance
    makes the selected values explicit for this invocation and avoids
    mutating process-wide configuration.  ``raw_prompt`` is enabled by
    default because callers using this contract are passing user text
    directly to the provider.
    """

    model: Optional[str] = None
    effort: Optional[str] = None
    cwd: Optional[Path] = None
    # Canonical public permission contract for the development lane.
    permission_mode: str = "native_default"
    provider_state_root: Optional[Path] = None
    sandbox: Optional[str | bool] = None
    native_session_id: Optional[str] = None
    native_session_action: str = "stateless"
    ephemeral: Optional[bool] = None
    # Number of provider attempts for the manual lane.  One is deliberately
    # the default: a failed native resume is never converted to a stateless
    # request by this layer.
    retry_count: int = 1
    raw_prompt: bool = True
    # Pinned capability ceiling.  ``None`` keeps the backend's legacy tools;
    # a value narrower than read/write/execute must be enforced by argv.
    allowed_capabilities: Optional[tuple[str, ...]] = None

    @property
    def effective_approval_policy(self) -> str:
        value = str(self.permission_mode or "native_default").strip()
        aliases = {
            "provider_supported": "standard",
            "require": "standard",
            "always_ask": "standard",
            "default": "standard",
            "acceptEdits": "accept_edits",
            "accept-edits": "accept_edits",
        }
        return aliases.get(value, value)

    @property
    def effective_effort(self) -> Optional[str]:
        return self.effort


def _windows_hidden_creationflags() -> int:
    """Return the Windows flag used for every non-interactive CLI child."""

    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)


@dataclass(frozen=True)
class CLISessionCapabilities:
    """Provider-neutral description of native CLI session support."""

    # A backend must opt in explicitly.  The default remains stateless for
    # providers whose output only resembles a session but cannot be resumed
    # safely from a non-interactive process.
    native_sessions: bool = False
    supports_resume: bool = False
    supports_follow_up: bool = False
    fallback_to_stateless: bool = True
    supports_explicit_session_id: bool = False
    supports_detach: bool = False
    # Some providers (Antigravity in particular) allocate the conversation
    # handle only after a successful JSON response.  A successful turn
    # without that handle cannot be persisted as a resumable session.
    requires_output_session_id: bool = False


def _decode_cli_output(data: bytes | str | None) -> str:
    """Decode CLI output without letting console encoding mismatches crash a turn."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    candidates = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() != "utf-8":
        candidates.append(preferred)
    candidates.append("cp932")

    seen = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue

    return data.decode("utf-8", errors="replace")


def _scoped_output_limit(scope: Any, *, default: int = 32_768) -> int:
    """Return the server-issued capture limit without trusting a lower scope."""

    try:
        from ...security.harness_execution_scope import (
            get_current_harness_execution_scope,
        )

        upper = get_current_harness_execution_scope()
        limits = getattr(upper, "resource_limits", None)
        value = getattr(limits, "max_output_bytes", default)
        return max(1, int(value))
    except (ImportError, TypeError, ValueError):
        return max(1, int(default))


def _append_bounded_bytes(buffer: bytearray, chunk: bytes, limit: int) -> None:
    buffer.extend(chunk)
    overflow = len(buffer) - limit
    if overflow > 0:
        del buffer[:overflow]


def _create_windows_process_job(process: subprocess.Popen) -> int | None:
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
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
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
        logger.debug("Failed to create Windows CLI kill job", exc_info=True)
        return None


def _close_windows_process_job(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            wintypes.HANDLE(job_handle)
        )
    except Exception:
        logger.debug("Failed to close Windows CLI kill job", exc_info=True)


def _stop_windows_process_job(job_handle: int | None) -> bool:
    """Confirm that an owned kill job has zero active processes before close."""
    if os.name != "nt" or not job_handle:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        job = wintypes.HANDLE(job_handle)
        if not kernel.TerminateJobObject(job, 1):
            return False
        deadline = time.monotonic() + 1.0
        while True:
            info = Accounting()
            if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            if info.TotalProcesses > 0 and info.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
    except Exception:
        logger.debug("Could not confirm Windows CLI job termination", exc_info=True)
        return False


def _resume_suspended_windows_cli(process: subprocess.Popen) -> bool:
    """Resume the unique primary thread after its process is assigned a Job.

    Popen closes its primary thread handle. Toolhelp identifies that thread
    without relying on undocumented native resume APIs.
    """
    try:
        import ctypes
        from ctypes import wintypes
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
        kernel.GetProcessIdOfThread.restype = wintypes.DWORD
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        thread_ids = []
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread_ids.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18 or len(thread_ids) != 1:
                return False
            thread = kernel.OpenThread(0x0802, False, thread_ids[0])
            if not thread:
                return False
            try:
                if process.poll() is not None or kernel.GetProcessIdOfThread(thread) != process.pid:
                    return False
                return kernel.ResumeThread(thread) == 1
            finally:
                kernel.CloseHandle(thread)
        finally:
            kernel.CloseHandle(snapshot)
    except Exception:
        logger.debug("Could not resume suspended Windows CLI primary thread", exc_info=True)
        return False


def _terminate_cli_process_tree(
    process: subprocess.Popen,
    *,
    windows_job_handle: int | None = None,
) -> None:
    """Stop the CLI wrapper and any child process it launched."""
    stopped_windows_job = None
    if os.name == "nt":
        if _stop_windows_process_job(windows_job_handle):
            stopped_windows_job = windows_job_handle
        _close_windows_process_job(windows_job_handle)
        try:
            if process.poll() is None:
                taskkill_result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2.0,
                    creationflags=_windows_hidden_creationflags(),
                )
                if taskkill_result.returncode != 0 and process.poll() is None:
                    logger.warning(
                        "taskkill could not terminate CLI process tree %s",
                        process.pid,
                    )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "Failed to terminate CLI process tree with taskkill",
                exc_info=True,
            )
    else:
        process_group_id = process.pid
        try:
            process_group_id = os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            logger.debug(
                "Failed to terminate CLI process group gracefully",
                exc_info=True,
            )
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            logger.debug(
                "Failed to kill CLI process group",
                exc_info=True,
            )

    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("CLI process %s did not exit after cancellation", process.pid)
    receipt = current_cli_lease_receipt()
    if receipt is not None:
        receipt.confirm_exit(process, stopped_windows_job=stopped_windows_job)


class CLIBackendBase(ABC):
    """
    Abstract base class for CLI-based LLM backends

    Subclasses must implement:
    - get_cli_command(prompt): Return CLI command with prompt as argument
    - get_provider_name(): Return provider name for logging
    - parse_output(raw_output): Filter/transform CLI output
    """

    prompt_stdin_supported = True
    direct_prompt_max_length = _MAX_ARG_LENGTH

    # A scoped CLI invocation is only considered safe when this generic
    # backend path is used.  Provider-specific wrappers may still override
    # ``execute_prompt`` for formatting, but they must delegate to
    # ``super().execute_prompt`` so the active AgentRunScope cannot fall back
    # to a host ``cwd``/``Popen`` process.
    supports_scoped_run = True
    # Provider wrappers that override ``execute_prompt`` must explicitly
    # attest that they delegate back to this method.  A custom override that
    # silently starts its own host process is denied by Specialist delegation.
    scoped_execution_delegate = False
    # Provider-specific opt-in for native session continuation inside the
    # scoped backend. The default remains denied because a resumed provider
    # session may retain host state that AoiTalk cannot fence.
    supports_scoped_native_sessions = False
    scoped_native_supports_explicit_session_id = False
    provider_state_env_name: Optional[str] = None
    provider_state_root_supported = False

    def __init__(self):
        """Initialize CLI backend"""
        self.provider_name = self.get_provider_name()
        self._last_usage: Optional[Dict[str, int]] = None
        self._usage_keys: set[str] = set()
        self._usage_invocation_id = 0
        self._active_native_session_action = "stateless"
        self._active_native_session_id: Optional[str] = None
        self._active_native_session_ephemeral: Optional[bool] = None
        self._last_native_session_id: Optional[str] = None
        self._last_native_resume_failure = False
        self._active_invocation_options: Optional[CLIInvocationOptions] = None
        self._last_invocation_config: Dict[str, Any] = {}
        self._last_cli_diagnostics: Dict[str, Any] = {}
        # Raw provider output is retained only for the backend's internal
        # parser.  ``get_last_cli_diagnostics`` never exposes these buffers.
        self._last_cli_raw_stdout = ""
        self._last_cli_raw_stderr = ""
        logger.info(f"[{self.provider_name}] Backend initialized")

    def get_invocation_options(self) -> Optional[CLIInvocationOptions]:
        """Return the options bound to the current provider invocation."""

        return self._active_invocation_options

    def get_last_invocation_config(self) -> Dict[str, Any]:
        """Return safe effective launch metadata without prompt text.

        Prompt text and native session identifiers are intentionally omitted.
        The method is a copy so callers cannot mutate backend state between
        turns.
        """

        return dict(self._last_invocation_config)

    def get_last_cli_diagnostics(self) -> Dict[str, Any]:
        """Return provider status/diagnostics observed for the last launch."""

        return dict(self._last_cli_diagnostics)

    def validate_invocation_options(self, options: CLIInvocationOptions) -> None:
        """Validate common per-invocation controls before a child starts."""

        if not isinstance(options, CLIInvocationOptions):
            raise TypeError("invocation_options must be CLIInvocationOptions")
        action = str(options.native_session_action or "stateless").strip().lower()
        if action not in {"stateless", "start", "resume"}:
            raise ValueError(
                "native_session_action must be one of stateless, start, or resume"
            )
        if options.retry_count != 1:
            raise ValueError("retry_count must be exactly 1 for the development lane")
        if options.allowed_capabilities is not None and not getattr(self, "supports_capability_ceiling", False):
            raise ValueError(f"{self.provider_name} cannot enforce a narrowed capability ceiling")
        if options.provider_state_root is not None:
            root = Path(options.provider_state_root).expanduser()
            if not root.is_absolute():
                raise ValueError("provider_state_root must be an absolute path")
            if not bool(getattr(self, "provider_state_root_supported", False)):
                raise ValueError(
                    f"{self.provider_name} does not support a provider state root override"
                )
        policy = options.effective_approval_policy
        if policy not in {
            "native_default",
            "standard",
            "accept_edits",
        }:
            raise ValueError(f"unsupported approval policy: {policy}")

    def _set_last_invocation_config(
        self,
        cmd: List[str],
        *,
        cwd: Optional[Path],
        options: Optional[CLIInvocationOptions],
    ) -> None:
        """Record a redacted command and effective, non-secret fields."""

        applied_approval_policy, applied_permission_mode, applied_sandbox = (
            self._extract_applied_launch_fields(cmd)
        )
        applied_any = any(
            value is not None
            for value in (
                applied_approval_policy,
                applied_permission_mode,
                applied_sandbox,
            )
        )
        self._last_invocation_config = {
            "provider": self.provider_name,
            "executable": str(cmd[0]) if cmd else "",
            "cwd": str(cwd) if cwd is not None else None,
            "model": (
                options.model
                if options is not None
                else getattr(self, "_model", None)
            ),
            "model_source": (
                "invocation"
                if options is not None and options.model
                else "backend_instance_or_environment"
                if options is None
                else "provider_default"
            ),
            "effort": (
                options.effective_effort
                if options is not None
                else getattr(self, "_reasoning_effort", None)
            ),
            "effort_source": (
                "invocation"
                if options is not None and options.effective_effort
                else "backend_instance_or_environment"
                if options is None
                else "provider_default"
            ),
            "approval_policy": (
                options.effective_approval_policy
                if options is not None
                else "ambient_generation_policy"
            ),
            "permission_mode": (
                options.effective_approval_policy
                if options is not None
                else "ambient_generation_policy"
            ),
            "applied_approval_policy": applied_approval_policy,
            "applied_permission_mode": applied_permission_mode,
            "applied_sandbox": applied_sandbox,
            "applied_source": (
                "explicit_launch_flags"
                if applied_any
                else "provider_saved_settings_unknown"
                if options is not None
                else "ambient_provider_settings"
            ),
            "sandbox": options.sandbox if options is not None else None,
            "sandbox_source": "invocation" if options is not None else "ambient_provider_settings",
            "state_root": (
                str(options.provider_state_root)
                if options is not None and options.provider_state_root is not None
                else None
            ),
            "state_root_source": (
                "invocation"
                if options is not None and options.provider_state_root is not None
                else "native_user_default"
                if options is not None
                else "ambient_provider_settings"
            ),
            "native_session_action": (
                getattr(self, "_active_native_session_action", "stateless")
            ),
            "source": "invocation_options" if options is not None else "legacy_defaults",
        }

    @staticmethod
    def _extract_applied_launch_fields(
        cmd: List[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract whitelisted effective flags without exposing argv."""

        approval: Optional[str] = None
        permission: Optional[str] = None
        sandbox: Optional[str] = None
        index = 0
        while index < len(cmd):
            flag = str(cmd[index])
            if flag == "-c" and index + 1 < len(cmd):
                value = str(cmd[index + 1])
                if value.startswith("approval_policy="):
                    value = value.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        approval = value[:80]
                index += 2
                continue
            if flag in {"--permission-mode", "--mode"} and index + 1 < len(cmd):
                permission = str(cmd[index + 1])[:80]
                index += 2
                continue
            if flag == "--sandbox":
                if index + 1 < len(cmd) and not str(cmd[index + 1]).startswith("-"):
                    sandbox = str(cmd[index + 1])[:80]
                    index += 2
                else:
                    sandbox = "enabled"
                    index += 1
                continue
            index += 1
        return approval, permission, sandbox

    def _record_observed_launch_fields(self, **fields: Any) -> None:
        """Merge provider-confirmed non-secret launch fields into metadata."""

        allowed = {"cwd", "model", "effort", "approval_policy", "permission_mode"}
        for key, value in fields.items():
            if key not in allowed or value in (None, ""):
                continue
            text = str(value).strip()
            if text and len(text) <= 1024 and not any(char in text for char in ("\x00", "\r", "\n")):
                self._last_invocation_config[f"observed_{key}"] = text
        if self._last_cli_diagnostics:
            self._refresh_safe_cli_diagnostics()

    def _record_cli_diagnostics(
        self,
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
    ) -> None:
        self._last_cli_raw_stdout = str(stdout or "")[-_MAX_INTERNAL_CLI_TEXT:]
        self._last_cli_raw_stderr = str(stderr or "")[-_MAX_INTERNAL_CLI_TEXT:]
        self._last_cli_diagnostics = {
            "returncode": int(returncode) if returncode is not None else None,
            "status": "running",
            "state": None,
            "error_code": None,
            "permission_event_count": 0,
            "input_event_count": 0,
            "stderr_present": bool(stderr),
        }
        self._refresh_safe_cli_diagnostics()

    def _set_cli_diagnostic(
        self,
        *,
        status: Optional[str] = None,
        state: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        if status:
            self._last_cli_diagnostics["status"] = str(status)[:80]
        if state:
            self._last_cli_diagnostics["state"] = str(state)[:80]
        if error_code:
            self._last_cli_diagnostics["error_code"] = str(error_code)[:120]
        self._refresh_safe_cli_diagnostics()

    def _refresh_safe_cli_diagnostics(self) -> None:
        """Add only bounded, non-content launch fields to diagnostics."""

        def safe(value: Any) -> Any:
            if value in (None, ""):
                return None
            text = str(value).strip()
            if not text or any(char in text for char in ("\x00", "\r", "\n")):
                return None
            return text[:1024]

        config = self._last_invocation_config
        self._last_cli_diagnostics["effective_model"] = safe(
            config.get("observed_model") or config.get("model")
        )
        self._last_cli_diagnostics["effective_cwd"] = safe(
            config.get("observed_cwd") or config.get("cwd")
        )
        self._last_cli_diagnostics["permission_mode"] = safe(
            config.get("observed_permission_mode")
            or config.get("permission_mode")
        )
        for field in (
            "applied_approval_policy",
            "applied_permission_mode",
            "applied_sandbox",
        ):
            self._last_cli_diagnostics[field] = safe(config.get(field))
        self._last_cli_diagnostics["applied_source"] = safe(
            config.get("applied_source")
        )
        self._last_cli_diagnostics["state_root"] = safe(
            config.get("observed_state_root") or config.get("state_root")
        )
        self._last_cli_diagnostics["state_root_source"] = safe(
            config.get("state_root_source")
        )

    def validate_success_output(self, raw_output: str, stderr: str) -> Optional[str]:
        """Return a provider error when a zero exit code is not success."""

        return None

    def observe_effective_launch_config(self, raw_output: str, stderr: str) -> None:
        """Capture provider-confirmed safe launch fields when available."""

        return None

    def get_session_capabilities(self) -> CLISessionCapabilities:
        """Return the native session contract implemented by this backend."""
        return CLISessionCapabilities()

    def supports_isolated_tool_free_invocation(self) -> bool:
        """Whether Help can invoke this CLI with all native tools disabled.

        The default is deliberately conservative. Provider CLIs may expose
        shell/filesystem tools, native MCP configuration, or user-level
        plugins that AoiTalk cannot revoke per request. A backend must opt in
        only after proving that every such capability is disabled for the
        invocation, including native sessions and global/user configuration.
        """

        return False

    @staticmethod
    def cli_help_contains(
        binary: str,
        help_args: List[str],
        *required_markers: str,
    ) -> bool:
        """Read a local CLI help page without starting a generation."""
        resolved = shutil.which(str(binary))
        if not resolved:
            return False
        try:
            result = subprocess.run(
                [resolved, *help_args],
                capture_output=True,
                check=False,
                timeout=5,
                env=build_aoitalk_subprocess_env(),
                creationflags=_windows_hidden_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        def decode(value: Any) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value or "")

        help_text = f"{decode(result.stdout)}\n{decode(result.stderr)}".lower()
        return all(str(marker).lower() in help_text for marker in required_markers)

    def create_native_session_id(self) -> Optional[str]:
        """Return an explicit provider session id when the CLI supports it."""
        return None

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        """Extract a stable provider-owned session id from raw output.

        Providers should override this when their JSON schema is known.  The
        generic implementation only accepts explicit, well-named fields and
        never treats an arbitrary event ``id`` as a conversation id.
        """
        import json

        keys = {
            "session_id",
            "sessionId",
            "thread_id",
            "threadId",
            "conversation_id",
            "conversationId",
        }

        def walk(value: Any) -> Optional[str]:
            if isinstance(value, dict):
                for key in keys:
                    candidate = value.get(key)
                    if isinstance(candidate, (str, int)) and str(candidate).strip():
                        return str(candidate).strip()
                for child in value.values():
                    found = walk(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found:
                        return found
            return None

        for line in str(raw_output or "").splitlines():
            try:
                parsed = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            found = walk(parsed)
            if found:
                return found
        return None

    def consume_last_native_session_id(self) -> Optional[str]:
        value = self._last_native_session_id
        self._last_native_session_id = None
        return value

    def was_native_session_resume_failure(self) -> bool:
        return bool(self._last_native_resume_failure)

    def is_native_session_resume_failure(self, error_text: str) -> bool:
        """Classify only stale/unknown-session errors as resumable failures."""
        lowered = str(error_text or "").lower()
        markers = (
            "session not found",
            "conversation not found",
            "thread not found",
            "unknown session",
            "unknown conversation",
            "unknown thread",
            "does not exist",
            "no such session",
            "invalid session",
            "invalid conversation",
        )
        if any(marker in lowered for marker in markers):
            return True
        has_session_term = any(
            term in lowered for term in ("session", "conversation", "thread")
        )
        return has_session_term and any(
            marker in lowered
            for marker in ("not found", "could not find", "no longer available")
        )

    def detach_native_session(self, native_session_id: str) -> bool:
        """Best-effort provider hook; most CLIs need no explicit cleanup."""
        return False

    @staticmethod
    def normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
        """CLIごとのusageをAgentRun/token tracking共通形式へ正規化する。"""
        if not isinstance(usage, dict):
            return None

        def _token_count(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if value is None:
                    continue
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    continue
            return 0

        def _optional_token_count(*keys: str) -> Optional[int]:
            for key in keys:
                if key not in usage or usage.get(key) is None:
                    continue
                try:
                    return max(0, int(usage[key]))
                except (TypeError, ValueError):
                    continue
            return None

        input_tokens = _token_count("input_tokens", "prompt_tokens")
        output_tokens = _token_count("output_tokens", "completion_tokens")
        cache_read = _token_count(
            "cache_read_input_tokens", "cached_input_tokens", "cached_tokens"
        )
        cache_creation = _token_count("cache_creation_input_tokens")
        # Anthropic CLIではcache read/createがinput_tokensと別枠。
        if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
            input_tokens += cache_read + cache_creation
        cached_tokens = cache_read
        explicit_total = _optional_token_count("total_tokens", "total")
        if not (input_tokens or output_tokens or explicit_total is not None):
            return None
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": (
                explicit_total
                if explicit_total is not None
                else input_tokens + output_tokens
            ),
        }

    def set_last_usage(
        self,
        usage: Any,
        *,
        usage_key: str | None = None,
    ) -> Optional[Dict[str, int]]:
        """Normalize and accumulate one provider-confirmed usage record.

        ``usage_key`` lets streaming and final-output parsing observe the same
        provider event without counting it twice.
        """
        normalized = self.normalize_usage(usage)
        if normalized is None:
            return None

        key = usage_key or (
            f"usage:{self._usage_invocation_id}:{len(self._usage_keys)}"
        )
        if key in self._usage_keys:
            return normalized
        self._usage_keys.add(key)

        if self._last_usage is None:
            self._last_usage = dict(normalized)
        else:
            for field in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "total_tokens",
            ):
                self._last_usage[field] = int(self._last_usage.get(field, 0)) + int(
                    normalized.get(field, 0)
                )
        return normalized

    def consume_last_usage(self) -> Optional[Dict[str, int]]:
        usage = self._last_usage
        self._last_usage = None
        self._usage_keys.clear()
        return usage

    @abstractmethod
    def get_cli_command(self, prompt: str) -> List[str]:
        """
        Get CLI command with prompt included as argument

        Args:
            prompt: The prompt text to send to the CLI

        Returns:
            List of command parts (e.g., ["agy", "-p", prompt])
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """
        Get provider name for logging

        Returns:
            Provider name (e.g., "Antigravity CLI")
        """
        pass

    def parse_output(self, raw_output: str) -> str:
        """
        Parse and filter CLI output

        Subclasses can override to handle provider-specific output formats
        (e.g., JSON parsing for Claude Code).

        Args:
            raw_output: Raw stdout from CLI

        Returns:
            Cleaned output text
        """
        return raw_output.strip()

    def parse_error_output(self, stdout: str, stderr: str, exit_code: int) -> Optional[str]:
        """
        Parse provider-specific error output into a user-facing message.

        CLI tools that emit structured events on failure can override this to avoid
        leaking raw JSONL or unrelated wrapper stderr into chat responses.
        """
        return None

    def get_subprocess_env(self) -> Dict[str, str]:
        """Return the AoiTalk runtime environment for CLI subprocesses.

        Every provider inherits the Python installation that is actually
        running AoiTalk.  Provider overrides should start with this mapping
        and then apply only their provider-specific changes.
        """
        options = self.get_invocation_options()
        if options is None:
            return build_aoitalk_subprocess_env()

        extra_env: Dict[str, str] = {}
        state_root = options.provider_state_root
        state_env_name = str(getattr(self, "provider_state_env_name", "") or "").strip()
        if state_root is not None and state_env_name:
            resolved_root = Path(state_root).expanduser().resolve()
            extra_env[state_env_name] = str(resolved_root)
            self._last_invocation_config["state_root"] = str(resolved_root)
            self._last_invocation_config["state_root_source"] = "invocation"

        inherit_keys = _WINDOWS_USER_STATE_ENV_NAMES if os.name == "nt" else ()
        env = build_aoitalk_subprocess_env(
            extra_env=extra_env,
            inherit_keys=inherit_keys,
        )
        self._refresh_safe_cli_diagnostics()
        return env

    def get_scoped_provider_auth(self) -> Optional[Tuple[str, str]]:
        """Return one provider auth value for the scoped guest, if required.

        The default is credential-free.  A provider that opts in must return
        one server-selected environment name and value.  The value is sent
        over the child stdin bootstrap, never in command-line arguments or
        the host environment passed to ``wsl.exe``.
        """

        return None

    def get_scoped_provider_env(self) -> Dict[str, str]:
        """Return additional provider-child values for the scoped channel."""

        return {}

    def get_scoped_guest_env(self) -> Dict[str, str]:
        """Return non-secret provider settings for the scoped guest."""

        return {}

    @staticmethod
    def _bind_scoped_provider_auth(
        stdin_input: bytes | None,
        guest_env: Dict[str, str],
        auth: Optional[Tuple[str, str] | Mapping[str, str]],
    ) -> tuple[bytes | None, Dict[str, str]]:
        """Bind provider values through stdin while keeping them out of argv."""

        if auth is None:
            return stdin_input, guest_env
        if isinstance(auth, tuple):
            if len(auth) != 2:
                raise RuntimeError("scoped provider authentication contract is invalid")
            values = {str(auth[0]): str(auth[1])}
        elif isinstance(auth, Mapping):
            values = {str(key): str(value) for key, value in auth.items()}
        else:
            raise RuntimeError("scoped provider authentication contract is invalid")
        if not values or any(key not in _SCOPED_PROVIDER_ENV_NAMES for key in values):
            raise RuntimeError("scoped provider environment name is not approved")
        for key, value in values.items():
            if not value or any(char in value for char in ("\x00", "\r", "\n")):
                raise RuntimeError(f"scoped provider environment value is invalid: {key}")
            if len(value) > 8192:
                raise RuntimeError(f"scoped provider environment value is too long: {key}")
        if "AOITALK_PROVIDER_AUTH_STDIN" in guest_env:
            raise RuntimeError("scoped provider authentication marker is already set")
        lines = [_SCOPED_PROVIDER_ENV_MARKER]
        for key in sorted(values):
            encoded = base64.b64encode(values[key].encode("utf-8")).decode("ascii")
            lines.append(f"{key} {encoded}")
        lines.append("AOITALK_END")
        prefix = ("\n".join(lines) + "\n").encode("ascii")
        bound_input = prefix + (stdin_input or b"")
        bound_env = dict(guest_env)
        # This marker is metadata only. Values are consumed from stdin by the
        # fixed WSL bootstrap and are absent from its argv and files.
        bound_env["AOITALK_PROVIDER_AUTH_STDIN"] = _SCOPED_PROVIDER_ENV_MARKER
        return bound_input, bound_env

    def get_cwd_args(self, cwd: Optional[Path]) -> List[str]:
        """Return provider-specific working-directory arguments.

        Most CLIs inherit the subprocess working directory.  A provider may
        additionally require its explicit ``--cwd`` option for headless mode.
        """
        return []

    def handle_stream_output_line(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        """Translate a streamed stdout line into UI progress events.

        Provider backends can override this for structured output such as JSONL.
        The raw line is still retained for final parsing regardless of whether
        an event is emitted.
        """
        return None

    def _emit_explicit_waiting_state(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        """Forward only provider-declared permission/input wait states.

        A generic parser must not infer a wait from ordinary prose.  It only
        emits when a structured JSON event names a known permission or input
        state, preserving the raw event for an upper service that owns the
        response policy.
        """

        import json

        try:
            event = json.loads(str(line or "").strip())
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return

        permission_markers = {
            "permission_request",
            "permission_required",
            "approval_required",
            "needs_approval",
            "waiting_permission",
            "permission_pending",
        }
        input_markers = {
            "input_required",
            "user_input_required",
            "waiting_for_input",
            "input_pending",
            "ask_user",
        }

        def walk(value: Any):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        candidates = list(walk(event))
        normalized_values = {
            str(value).strip().lower().replace("-", "_")
            for candidate in candidates
            for key in ("event", "type", "subtype", "status", "state")
            for value in [candidate.get(key)]
            if isinstance(value, str)
        }
        if normalized_values & permission_markers:
            self._last_cli_diagnostics["permission_event_count"] = int(
                self._last_cli_diagnostics.get("permission_event_count", 0)
            ) + 1
            self._set_cli_diagnostic(status="waiting", state="waiting_approval")
            event_callback(
                "permission_required",
                {"provider": self.provider_name, "event": event},
            )
            return
        # AGY documents WAITING as a terminal state while it waits on input;
        # only accept the uppercase/structured value, never natural language.
        if normalized_values & input_markers or "waiting" in normalized_values:
            self._last_cli_diagnostics["input_event_count"] = int(
                self._last_cli_diagnostics.get("input_event_count", 0)
            ) + 1
            self._set_cli_diagnostic(status="waiting", state="waiting_input")
            event_callback(
                "input_required",
                {"provider": self.provider_name, "event": event},
            )

    def parse_tool_calls(self, cli_output: str) -> List[Dict[str, Any]]:
        """
        Parse tool calls from CLI output

        Subclasses can override to detect and parse tool call requests.
        Default parses the shared [TOOL_CALL: ...] format.

        Args:
            cli_output: Parsed CLI output text

        Returns:
            List of tool call dicts, empty if no tool calls detected
        """
        from ...tools.adapters import CLIAdapter

        return CLIAdapter.parse_tool_calls(cli_output)

    def execute_prompt(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
        extra_args: Optional[List[str]] = None,
        system_context: Optional[str] = None,
        event_callback: Optional[CLIEventCallback] = None,
        *,
        native_session_id: Optional[str] = None,
        native_session_action: str = "stateless",
        ephemeral: Optional[bool] = None,
        invocation_options: Optional[CLIInvocationOptions] = None,
    ) -> Tuple[bool, str]:
        """
        Execute prompt via CLI

        Args:
            prompt: Prompt to execute (user message or full prompt)
            cwd: Working directory
            timeout: Timeout in seconds. None means wait until the CLI exits.
            extra_args: Additional CLI arguments (e.g., MCP config)
            system_context: System context to pass via stdin (instructions, history, tools).
                           When provided, prompt is passed via CLI argument and
                           system_context via stdin for backends that support it.

        Returns:
            (success: bool, output: str)
        """
        options = invocation_options
        if options is not None:
            try:
                self.validate_invocation_options(options)
            except (TypeError, ValueError, AttributeError) as exc:
                return False, f"Invalid CLI invocation options: {exc}"
            if options.cwd is not None:
                cwd = Path(options.cwd)
            if native_session_id is None and options.native_session_id:
                native_session_id = options.native_session_id
            if native_session_action == "stateless" and options.native_session_action:
                native_session_action = options.native_session_action
            if ephemeral is None:
                ephemeral = options.ephemeral

        self._last_usage = None
        self._usage_keys.clear()
        receipt = current_cli_lease_receipt()
        mark_cli_backend_covered()
        self._usage_invocation_id += 1
        action = str(native_session_action or "stateless").strip().lower()
        if action not in {"stateless", "start", "resume"}:
            if options is not None:
                return False, "Invalid CLI invocation options: native_session_action must be one of stateless, start, or resume"
            action = "stateless"
        # Resolve the scope before any provider capability probe.  Codex,
        # Claude, and Grok discover native-session support by running their
        # host CLI ``--help`` command; that probe is itself forbidden while a
        # trusted repository scope is active.
        active_scope = self._active_run_scope()
        if active_scope is None and action in {"start", "resume"}:
            capabilities = self.get_session_capabilities()
        elif (
            active_scope is not None
            and action in {"start", "resume"}
            and bool(getattr(self, "supports_scoped_native_sessions", False))
        ):
            # Host ``--help`` probes are forbidden in a scoped run. A
            # provider opts into this static contract only when its cache root
            # and session flags are independently fenced by the backend.
            capabilities = CLISessionCapabilities(
                native_sessions=True,
                supports_resume=True,
                supports_follow_up=True,
                fallback_to_stateless=True,
                supports_explicit_session_id=bool(
                    getattr(self, "scoped_native_supports_explicit_session_id", False)
                ),
            )
        else:
            capabilities = CLISessionCapabilities()
        self._active_native_session_action = action
        self._active_native_session_id = (
            str(native_session_id).strip() if native_session_id else None
        )
        self._active_native_session_ephemeral = ephemeral
        self._last_native_session_id = None
        self._last_native_resume_failure = False
        self._active_invocation_options = options
        self._last_invocation_config = {}
        self._last_cli_diagnostics = {}
        if (
            options is not None
            and action in {"start", "resume"}
            and not capabilities.native_sessions
        ):
            self._last_invocation_config = {
                "model": options.model,
                "cwd": str(cwd) if cwd is not None else None,
                "permission_mode": options.effective_approval_policy,
            }
            self._record_cli_diagnostics(returncode=None, stdout="", stderr="")
            self._set_cli_diagnostic(
                status="failed",
                state="failed",
                error_code="native_session_capability_unavailable",
            )
            return (
                False,
                f"{self.provider_name} native session capability is unavailable",
            )
        if (
            action == "start"
            and capabilities.native_sessions
            and capabilities.supports_explicit_session_id
            and not self._active_native_session_id
        ):
            self._active_native_session_id = self.create_native_session_id()
        if ephemeral is None and action in {"start", "resume"} and capabilities.native_sessions:
            self._active_native_session_ephemeral = False
        if (
            active_scope is not None
            and action in {"start", "resume"}
            and not bool(getattr(self, "supports_scoped_native_sessions", False))
        ):
            return (
                False,
                f"{self.provider_name} scoped execution denied: native CLI sessions "
                "are unavailable in the WSL2/bubblewrap lane",
            )
        if action == "resume" and not self._active_native_session_id:
            return False, f"{self.provider_name} native resume requires a session id"
        if (
            action == "start"
            and self._active_native_session_id
            and capabilities.native_sessions
            and not capabilities.supports_explicit_session_id
        ):
            return False, f"{self.provider_name} does not accept an explicit native session id for start"
        direct_prompt_max_length = getattr(
            self,
            "direct_prompt_max_length",
            _MAX_ARG_LENGTH,
        )
        prompt_stdin_supported = getattr(self, "prompt_stdin_supported", True)
        raw_prompt = bool(options.raw_prompt) if options is not None else False
        if (
            options is not None
            and raw_prompt
            and not prompt_stdin_supported
            and len(prompt) > direct_prompt_max_length
        ):
            return (
                False,
                f"{self.provider_name} raw prompt exceeds the direct argument limit "
                f"({direct_prompt_max_length} characters); stdin is unavailable",
            )

        if system_context:
            # system_context → stdin, prompt → -p
            if not prompt_stdin_supported:
                combined_prompt = f"{system_context}\n\n{prompt}" if prompt else system_context
                cmd = self.get_cli_command(combined_prompt)
                stdin_input = None
                logger.info(
                    f"[{self.provider_name}] Using direct argument for system_context + prompt"
                )
            elif len(prompt) > direct_prompt_max_length:
                # User message too long for -p, concatenate into stdin
                cmd = self.get_cli_command("")
                stdin_input = f"{system_context}\n\n{prompt}"
                logger.info(f"[{self.provider_name}] Using stdin for system_context + prompt")
            else:
                cmd = self.get_cli_command(prompt)
                stdin_input = system_context
                logger.info(f"[{self.provider_name}] Using stdin for system_context, -p for user prompt")
        elif len(prompt) > direct_prompt_max_length and prompt_stdin_supported:
            # プロンプトが長すぎる場合、stdinにフォールバック
            cmd = self.get_cli_command("")
            stdin_input = prompt
            logger.info(f"[{self.provider_name}] Prompt too long ({len(prompt)} chars), using stdin")
        else:
            cmd = self.get_cli_command(prompt)
            stdin_input = None

        # MCP config等の追加引数。アクティブな AgentRunScope では、
        # provider 側の host パス（例: Grok の ``--cwd D:\\...``）を
        # コマンドへ混ぜず、WSL+bwrap の ``--chdir /workspace`` を唯一の
        # 作業ディレクトリ境界として使う。
        cwd_args = [] if active_scope is not None else self.get_cwd_args(cwd)
        if cwd_args:
            cmd.extend(cwd_args)
        if extra_args:
            cmd.extend(extra_args)

        self._set_last_invocation_config(cmd, cwd=cwd, options=options)

        if active_scope is not None:
            # Never resolve a provider executable with the host PATH in this
            # branch.  ``shutil.which`` can turn a bare command into an
            # absolute Windows path which is not visible inside the mounted
            # WSL workspace (and would make a future fallback tempting).
            return self._execute_prompt_scoped(
                cmd,
                stdin_input=(
                    stdin_input.encode("utf-8") if stdin_input is not None else None
                ),
                cwd=cwd,
                env=self.get_subprocess_env(),
                timeout=timeout,
                event_callback=event_callback,
                scope=active_scope,
                action=action,
                capabilities=capabilities,
            )

        # Windows では .cmd/.bat ラッパーを subprocess が見つけられないため
        # shutil.which() で PATHEXT を考慮したフルパス解決を行う
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved
        self._set_last_invocation_config(cmd, cwd=cwd, options=options)

        logger.info(f"[{self.provider_name}] Executing: {cmd[0]}")
        logger.debug(f"[{self.provider_name}] Prompt length: {len(prompt)} chars")
        subprocess_env = self.get_subprocess_env()

        # The caller's OutboundPrivacyGateway owns approval and retry
        # semantics.  Once an invocation is inside that transaction, an
        # implicit subprocess retry would be a second provider send that the
        # user never approved.  Keep the historical retry budget for direct
        # legacy callers, but make the gated path one-shot.
        if getattr(self, "_aoitalk_egress_transaction", False):
            max_retries = 1
        elif options is not None:
            max_retries = int(options.retry_count)
        else:
            max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                stdin_input_bytes = (
                    stdin_input.encode("utf-8") if stdin_input is not None else None
                )
                cancellation_handle = get_current_generation_cancellation()
                if event_callback or cancellation_handle is not None or receipt is not None:
                    returncode, stdout_text, stderr_text = self._run_streaming_process(
                        cmd,
                        stdin_input_bytes=stdin_input_bytes,
                        cwd=cwd,
                        env=subprocess_env,
                        timeout=timeout,
                        event_callback=event_callback or (lambda _type, _data: None),
                    )
                else:
                    result = subprocess.run(
                        cmd,
                        input=stdin_input_bytes,
                        cwd=str(cwd) if cwd else None,
                        capture_output=True,
                        check=False,
                        env=subprocess_env,
                        timeout=timeout,
                        creationflags=_windows_hidden_creationflags(),
                    )
                    returncode = result.returncode
                    stdout_text = _decode_cli_output(result.stdout)
                    stderr_text = _decode_cli_output(result.stderr)

                if returncode == 0:
                    self._record_cli_diagnostics(
                        returncode=returncode,
                        stdout=stdout_text,
                        stderr=stderr_text,
                    )
                    self.observe_effective_launch_config(stdout_text, stderr_text)
                    success_error = self.validate_success_output(stdout_text, stderr_text)
                    if success_error:
                        diagnostic_status = self._last_cli_diagnostics.get("status")
                        pending_state = self._last_cli_diagnostics.get("state")
                        if (
                            pending_state in {"waiting_input", "waiting_approval"}
                            and action in {"start", "resume"}
                            and capabilities.native_sessions
                        ):
                            pending_session_id = self.extract_native_session_id(stdout_text)
                            if (
                                action == "resume"
                                and self._active_native_session_id
                                and pending_session_id
                                and pending_session_id != self._active_native_session_id
                            ):
                                self._set_cli_diagnostic(
                                    status="failed",
                                    state="failed",
                                    error_code="native_session_id_mismatch",
                                )
                                self._last_native_resume_failure = True
                                return (
                                    False,
                                    f"{self.provider_name} native resume returned a different session id",
                                )
                            if pending_session_id:
                                self._last_native_session_id = pending_session_id
                        self._set_cli_diagnostic(
                            status=("waiting" if diagnostic_status == "waiting" else "failed"),
                            state=self._last_cli_diagnostics.get("state") or "failed",
                            error_code=self._last_cli_diagnostics.get("error_code")
                            or "provider_output_invalid",
                        )
                        if action == "resume":
                            self._last_native_resume_failure = self.is_native_session_resume_failure(
                                success_error
                            )
                        return False, success_error
                    if action in {"start", "resume"} and capabilities.native_sessions:
                        extracted_session_id = self.extract_native_session_id(stdout_text)
                        if capabilities.requires_output_session_id and not extracted_session_id:
                            self._set_cli_diagnostic(
                                status="failed",
                                state="failed",
                                error_code="native_session_id_missing",
                            )
                            if action == "resume":
                                self._last_native_resume_failure = True
                            return (
                                False,
                                f"{self.provider_name} native {action} did not return a session id",
                            )
                        if (
                            action == "resume"
                            and self._active_native_session_id
                            and extracted_session_id
                            and extracted_session_id != self._active_native_session_id
                        ):
                            self._set_cli_diagnostic(
                                status="failed",
                                state="failed",
                                error_code="native_session_id_mismatch",
                            )
                            self._last_native_resume_failure = True
                            return (
                                False,
                                f"{self.provider_name} native resume returned a different session id",
                            )
                        self._last_native_session_id = (
                            extracted_session_id
                            or self._active_native_session_id
                        )
                    output = self.parse_output(stdout_text)
                    self._set_cli_diagnostic(status="success", state="completed")
                    logger.info(f"[{self.provider_name}] Execution successful: {len(output)} chars")
                    return True, output

                stderr = stderr_text.strip()
                stdout = stdout_text.strip()
                self._record_cli_diagnostics(
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                )
                self.observe_effective_launch_config(stdout_text, stderr_text)
                logger.warning(
                    f"[{self.provider_name}] Attempt {attempt+1}/{max_retries} "
                    f"failed (exit code {returncode})"
                )
                if stderr:
                    logger.warning(f"[{self.provider_name}] STDERR: {stderr[:500]}")
                if stdout:
                    logger.debug(f"[{self.provider_name}] STDOUT: {stdout[:200]}")

                # 一時的なネットワークエラーの場合はリトライ
                is_transient = any(
                    err in stderr
                    for err in ["ECONNRESET", "ETIMEDOUT", "Connection refused"]
                )

                if is_transient and attempt < max_retries - 1:
                    logger.info(
                        f"[{self.provider_name}] Retrying per transient error... "
                        f"(waiting {retry_delay}s)"
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue

                parsed_error = self.parse_error_output(stdout, stderr, returncode)
                if parsed_error:
                    error_msg = parsed_error
                    logger.error(f"[{self.provider_name}] {parsed_error}")
                else:
                    error_msg = f"CLI failed (exit code {returncode})"
                    if stderr:
                        error_msg += f"\nSTDERR: {stderr}"
                        logger.error(f"[{self.provider_name}] {stderr}")
                    if stdout:
                        error_msg += f"\nSTDOUT: {stdout}"
                        logger.warning(f"[{self.provider_name}] {stdout}")

                if action == "resume":
                    self._last_native_resume_failure = self.is_native_session_resume_failure(
                        error_msg
                    )

                self._set_cli_diagnostic(
                    status="failed",
                    state="failed",
                    error_code=f"cli_exit_{returncode}",
                )
                return False, error_msg

            except FileNotFoundError:
                error_msg = f"CLI not found: {cmd[0]}"
                self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
                self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_not_found")
                if action == "resume":
                    self._last_native_resume_failure = self.is_native_session_resume_failure(
                        error_msg
                    )
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except subprocess.TimeoutExpired:
                error_msg = f"CLI execution timed out ({timeout}s)"
                self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
                self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_timeout")
                if action == "resume":
                    self._last_native_resume_failure = self.is_native_session_resume_failure(
                        error_msg
                    )
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except GenerationCancelled:
                raise
            except GenerationInterrupted:
                raise
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
                self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
                self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_exception")
                if action == "resume":
                    self._last_native_resume_failure = self.is_native_session_resume_failure(
                        error_msg
                    )
                logger.error(f"[{self.provider_name}] {error_msg}", exc_info=True)
                return False, error_msg

        return False, "Max retries exceeded"

    @staticmethod
    def _active_run_scope() -> Any | None:
        """Read the trusted request-local repository scope, if one exists."""

        try:
            from ...security.agent_run_scope import get_current_run_scope

            return get_current_run_scope()
        except Exception:
            # Security integrations are optional in stripped/runtime builds;
            # an unavailable scope module must not break ordinary unscoped CLI
            # calls.  Scoped callers are denied by ``_execute_prompt_scoped``
            # when no concrete backend can be imported.
            return None

    def _execute_prompt_scoped(
        self,
        cmd: List[str],
        *,
        stdin_input: bytes | None,
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        timeout: Optional[int],
        event_callback: Optional[CLIEventCallback],
        scope: Any,
        action: str,
        capabilities: CLISessionCapabilities,
    ) -> Tuple[bool, str]:
        """Execute a CLI entirely through the verified WSL+bwrap backend.

        A ContextVar cannot constrain a hostile CLI process.  Therefore an
        active ``AgentRunScope`` never reaches the host ``subprocess.run`` or
        the generic ``Popen`` path.  The WSL backend owns process creation and
        mounts only the selected repository, including all descendants.
        """

        provider = str(self.provider_name or "CLI")
        try:
            from ...security.wsl_bwrap_backend import (
                WslBwrapError,
                get_wsl_bwrap_backend,
            )
            from ...security.agent_run_scope import AgentRunScope
        except Exception as exc:
            return False, f"{provider} scoped execution denied: sandbox backend unavailable ({exc})"

        if not isinstance(scope, AgentRunScope):
            return False, f"{provider} scoped execution denied: invalid AgentRunScope"
        if not bool(getattr(self, "supports_scoped_run", False)):
            return False, f"{provider} scoped execution denied: backend is not scope-capable"
        if not cmd:
            return False, f"{provider} scoped execution denied: CLI command is empty"

        scoped_guest_env: Dict[str, str] = {}
        try:
            scoped_guest_env = dict(self.get_scoped_guest_env() or {})
            scoped_provider_env = dict(self.get_scoped_provider_env() or {})
            scoped_auth = self.get_scoped_provider_auth()
            if scoped_auth is not None:
                if isinstance(scoped_auth, tuple):
                    scoped_provider_env.setdefault(
                        str(scoped_auth[0]), str(scoped_auth[1])
                    )
                elif isinstance(scoped_auth, Mapping):
                    for key, value in scoped_auth.items():
                        scoped_provider_env.setdefault(str(key), str(value))
                else:
                    raise RuntimeError("scoped provider authentication contract is invalid")
            scoped_stdin_input, scoped_guest_env = self._bind_scoped_provider_auth(
                stdin_input,
                scoped_guest_env,
                scoped_provider_env or None,
            )
            safe_cwd = scope.assert_command_cwd_allowed(
                scope.canonical_root if cwd is None else cwd
            )
            backend = get_wsl_bwrap_backend()
            if not bool(getattr(backend, "file_scoped", False)):
                raise WslBwrapError("configured backend is not file-scoped")
            if not backend.is_available():
                raise WslBwrapError(
                    "file-scoped WSL2/bubblewrap backend is unavailable"
                )
            # POSIX quoting is intentional: the command executes under
            # ``/bin/sh -lc`` inside WSL, not under the Windows shell.
            command = shlex.join(str(part) for part in cmd)
            logger.info(
                "[%s] Executing scoped CLI through WSL+bwrap (cwd=%s)",
                provider,
                safe_cwd,
            )
            cancellation_handle = get_current_generation_cancellation()
            if event_callback or cancellation_handle is not None or current_cli_lease_receipt() is not None:
                returncode, stdout_text, stderr_text = self._run_scoped_streaming_process(
                    backend,
                    scope,
                    command,
                    stdin_input_bytes=scoped_stdin_input,
                    cwd=safe_cwd,
                    env=env,
                    guest_env=scoped_guest_env,
                    timeout=timeout,
                    event_callback=event_callback or (lambda _type, _data: None),
                )
            else:
                process = backend.spawn(
                    scope,
                    command,
                    cwd=safe_cwd,
                    shell="bash",
                    timeout=timeout,
                    env=env,
                    guest_env=scoped_guest_env,
                    popen_kwargs={
                        "stdin": (
                            subprocess.PIPE
                            if scoped_stdin_input is not None
                            else subprocess.DEVNULL
                        ),
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": False,
                    },
                )
                try:
                    stdout, stderr = process.communicate(
                        input=scoped_stdin_input,
                        timeout=timeout,
                    )
                except subprocess.TimeoutExpired:
                    _terminate_cli_process_tree(process)
                    raise
                returncode = process.returncode
                stdout_text = _decode_cli_output(stdout)
                stderr_text = _decode_cli_output(stderr)
        except FileNotFoundError:
            error_msg = f"CLI not found inside scoped WSL backend: {cmd[0] if cmd else provider}"
            self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
            self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_not_found")
            if action == "resume":
                self._last_native_resume_failure = self.is_native_session_resume_failure(
                    error_msg
                )
            return False, error_msg
        except subprocess.TimeoutExpired:
            error_msg = f"CLI execution timed out ({timeout}s)"
            self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
            self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_timeout")
            if action == "resume":
                self._last_native_resume_failure = self.is_native_session_resume_failure(
                    error_msg
                )
            logger.error("[%s] %s", provider, error_msg)
            return False, error_msg
        except GenerationCancelled:
            raise
        except GenerationInterrupted:
            raise
        except Exception as exc:
            logger.error("[%s] Scoped CLI execution failed: %s", provider, exc)
            error_msg = f"{provider} scoped execution denied: {exc}"
            self._record_cli_diagnostics(returncode=None, stdout="", stderr=error_msg)
            self._set_cli_diagnostic(status="failed", state="failed", error_code="cli_exception")
            if action == "resume":
                self._last_native_resume_failure = self.is_native_session_resume_failure(
                    error_msg
                )
            return False, error_msg

        self._record_cli_diagnostics(
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )
        self.observe_effective_launch_config(stdout_text, stderr_text)
        if returncode == 0:
            success_error = self.validate_success_output(stdout_text, stderr_text)
            if success_error:
                diagnostic_status = self._last_cli_diagnostics.get("status")
                pending_state = self._last_cli_diagnostics.get("state")
                if (
                    pending_state in {"waiting_input", "waiting_approval"}
                    and action in {"start", "resume"}
                    and capabilities.native_sessions
                ):
                    pending_session_id = self.extract_native_session_id(stdout_text)
                    if (
                        action == "resume"
                        and self._active_native_session_id
                        and pending_session_id
                        and pending_session_id != self._active_native_session_id
                    ):
                        self._set_cli_diagnostic(
                            status="failed",
                            state="failed",
                            error_code="native_session_id_mismatch",
                        )
                        self._last_native_resume_failure = True
                        return (
                            False,
                            f"{self.provider_name} native resume returned a different session id",
                        )
                    if pending_session_id:
                        self._last_native_session_id = pending_session_id
                self._set_cli_diagnostic(
                    status=("waiting" if diagnostic_status == "waiting" else "failed"),
                    state=self._last_cli_diagnostics.get("state") or "failed",
                    error_code=self._last_cli_diagnostics.get("error_code")
                    or "provider_output_invalid",
                )
                if action == "resume":
                    self._last_native_resume_failure = self.is_native_session_resume_failure(
                        success_error
                    )
                return False, success_error
            if action in {"start", "resume"} and capabilities.native_sessions:
                extracted_session_id = self.extract_native_session_id(stdout_text)
                if capabilities.requires_output_session_id and not extracted_session_id:
                    self._set_cli_diagnostic(
                        status="failed",
                        state="failed",
                        error_code="native_session_id_missing",
                    )
                    if action == "resume":
                        self._last_native_resume_failure = True
                    return (
                        False,
                        f"{self.provider_name} native {action} did not return a session id",
                    )
                if (
                    action == "resume"
                    and self._active_native_session_id
                    and extracted_session_id
                    and extracted_session_id != self._active_native_session_id
                ):
                    self._set_cli_diagnostic(
                        status="failed",
                        state="failed",
                        error_code="native_session_id_mismatch",
                    )
                    self._last_native_resume_failure = True
                    return (
                        False,
                        f"{self.provider_name} native resume returned a different session id",
                    )
                self._last_native_session_id = (
                    extracted_session_id or self._active_native_session_id
                )
            output = self.parse_output(stdout_text)
            self._set_cli_diagnostic(status="success", state="completed")
            logger.info("[%s] Scoped execution successful: %s chars", provider, len(output))
            return True, output

        stderr = stderr_text.strip()
        stdout = stdout_text.strip()
        self.observe_effective_launch_config(stdout_text, stderr_text)
        parsed_error = self.parse_error_output(stdout, stderr, returncode)
        if parsed_error:
            error_msg = parsed_error
        else:
            error_msg = f"CLI failed (exit code {returncode})"
            if stderr:
                error_msg += f"\nSTDERR: {stderr}"
            if stdout:
                error_msg += f"\nSTDOUT: {stdout}"
        self._set_cli_diagnostic(
            status="failed",
            state="failed",
            error_code=f"cli_exit_{returncode}",
        )
        if action == "resume":
            self._last_native_resume_failure = self.is_native_session_resume_failure(
                error_msg
            )
        return False, error_msg

    def _run_scoped_streaming_process(
        self,
        backend: Any,
        scope: Any,
        command: str,
        *,
        stdin_input_bytes: Optional[bytes],
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        guest_env: Optional[Dict[str, str]],
        timeout: Optional[int],
        event_callback: CLIEventCallback,
    ) -> tuple[int, str, str]:
        """Stream a backend-owned WSL process without host ``Popen``."""

        max_output_bytes = _scoped_output_limit(scope)
        # A bounded queue applies pipe backpressure when event handling is
        # slower than the child.  Reading fixed-size chunks also avoids one
        # attacker-controlled no-newline record becoming a giant allocation.
        stdout_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        reader_stop = threading.Event()
        stdout_tail = bytearray()
        pending_line = bytearray()
        receipt = current_cli_lease_receipt()
        ticket = receipt.begin_spawn() if receipt is not None else None
        process = backend.spawn(
            scope,
            command,
            cwd=cwd,
            shell="bash",
            timeout=timeout,
            env=env,
            guest_env=guest_env,
            popen_kwargs={
                "stdin": (
                    subprocess.PIPE
                    if stdin_input_bytes is not None
                    else subprocess.DEVNULL
                ),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": False,
            },
        )
        if receipt is not None:
            # WSL publication finalization does not establish process-tree
            # termination. Keep an unknown scoped tree until it is proven.
            receipt.bind_process(ticket, process, isolated_group=False)

        def _reader() -> None:
            try:
                if process.stdout is None:
                    return
                for chunk in iter(lambda: process.stdout.read(8192), b""):
                    while not reader_stop.is_set():
                        try:
                            stdout_queue.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            finally:
                while not reader_stop.is_set():
                    try:
                        stdout_queue.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        if stdin_input_bytes is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_input_bytes)
                process.stdin.close()
            except BrokenPipeError:
                pass

        deadline = time.monotonic() + timeout if timeout is not None else None
        reader_done = False
        while not reader_done:
            cancellation_handle = get_current_generation_cancellation()
            if (
                cancellation_handle is not None
                and cancellation_handle.cancel_requested.is_set()
            ):
                reader_stop.set()
                _terminate_cli_process_tree(process)
                reader.join(timeout=1.0)
                raise GenerationCancelled("CLI generation cancelled")
            if (
                cancellation_handle is not None
                and cancellation_handle.interrupt_requested.is_set()
            ):
                reader_stop.set()
                _terminate_cli_process_tree(process)
                reader.join(timeout=1.0)
                raise_if_generation_interrupted()
            if deadline is None:
                queue_timeout = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reader_stop.set()
                    _terminate_cli_process_tree(process)
                    reader.join(timeout=1.0)
                    raise subprocess.TimeoutExpired(command, timeout)
                queue_timeout = min(0.1, max(remaining, 0.01))
            try:
                chunk = stdout_queue.get(timeout=queue_timeout)
            except queue.Empty:
                continue
            if chunk is None:
                reader_done = True
                continue
            _append_bounded_bytes(stdout_tail, chunk, max_output_bytes)
            pending_line.extend(chunk)
            while b"\n" in pending_line:
                raw_line, _, remainder = pending_line.partition(b"\n")
                pending_line = bytearray(remainder)
                line = _decode_cli_output(raw_line).rstrip("\r")
                if not line:
                    continue
                try:
                    self._emit_explicit_waiting_state(line, event_callback)
                    self.handle_stream_output_line(line, event_callback)
                except GenerationInterrupted:
                    reader_stop.set()
                    _terminate_cli_process_tree(process)
                    reader.join(timeout=1.0)
                    raise
                except Exception:
                    logger.debug(
                        "[%s] Scoped stream event handling failed",
                        self.provider_name,
                        exc_info=True,
                    )
            if len(pending_line) > max_output_bytes:
                del pending_line[:-max_output_bytes]

        return_code = process.wait()
        reader_stop.set()
        reader.join(timeout=1.0)
        if pending_line:
            line = _decode_cli_output(bytes(pending_line)).rstrip("\r\n")
            if line:
                try:
                    self._emit_explicit_waiting_state(line, event_callback)
                    self.handle_stream_output_line(line, event_callback)
                except GenerationInterrupted:
                    _terminate_cli_process_tree(process)
                    raise
                except Exception:
                    logger.debug(
                        "[%s] Scoped stream event handling failed",
                        self.provider_name,
                        exc_info=True,
                    )
        return return_code, _decode_cli_output(bytes(stdout_tail)), ""

    def _run_streaming_process(
        self,
        cmd: List[str],
        *,
        stdin_input_bytes: Optional[bytes],
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        timeout: Optional[int],
        event_callback: CLIEventCallback,
    ) -> tuple[int, str, str]:
        """Run a CLI process while forwarding stdout lines as progress events."""
        stdout_queue: queue.Queue[bytes | None] = queue.Queue()
        stdout_chunks: list[bytes] = []

        receipt = current_cli_lease_receipt()
        ticket = receipt.begin_spawn() if receipt is not None else None
        manual_options = self.get_invocation_options() is not None
        suspended_windows = os.name == "nt" and (receipt is not None or manual_options)
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
                env=env,
                start_new_session=os.name != "nt",
                creationflags=(
                    (
                        subprocess.CREATE_NEW_PROCESS_GROUP
                        | _windows_hidden_creationflags()
                        | (0x00000004 if suspended_windows else 0)
                    )
                    if os.name == "nt"
                    else 0
                ),
            )
        except OSError:
            if receipt is not None:
                receipt.no_process_started(ticket)
            raise
        if receipt is not None:
            receipt.bind_process(ticket, process, isolated_group=True)
        windows_job_handle = _create_windows_process_job(process)
        if receipt is not None:
            receipt.bind_windows_job(process, windows_job_handle, assigned_before_resume=suspended_windows and bool(windows_job_handle))
        if suspended_windows and (not windows_job_handle or not _resume_suspended_windows_cli(process)):
            _terminate_cli_process_tree(process, windows_job_handle=windows_job_handle)
            raise RuntimeError("Could not safely resume the owned Windows CLI process")

        def _reader() -> None:
            try:
                if process.stdout is None:
                    return
                for chunk in iter(process.stdout.readline, b""):
                    stdout_queue.put(chunk)
            finally:
                stdout_queue.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        if stdin_input_bytes is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_input_bytes)
                process.stdin.close()
            except BrokenPipeError:
                pass

        deadline = time.monotonic() + timeout if timeout is not None else None
        reader_done = False
        while not reader_done:
            cancellation_handle = get_current_generation_cancellation()
            if (
                cancellation_handle is not None
                and cancellation_handle.cancel_requested.is_set()
            ):
                _terminate_cli_process_tree(
                    process,
                    windows_job_handle=windows_job_handle,
                )
                reader.join(timeout=1.0)
                raise GenerationCancelled("CLI generation cancelled")
            if (
                cancellation_handle is not None
                and cancellation_handle.interrupt_requested.is_set()
            ):
                _terminate_cli_process_tree(
                    process,
                    windows_job_handle=windows_job_handle,
                )
                reader.join(timeout=1.0)
                raise_if_generation_interrupted()
            if deadline is None:
                queue_timeout = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_cli_process_tree(
                        process,
                        windows_job_handle=windows_job_handle,
                    )
                    reader.join(timeout=1.0)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                queue_timeout = min(0.1, max(remaining, 0.01))
            try:
                chunk = stdout_queue.get(timeout=queue_timeout)
            except queue.Empty:
                continue
            if chunk is None:
                reader_done = True
                continue
            stdout_chunks.append(chunk)
            line = _decode_cli_output(chunk).rstrip("\r\n")
            if not line:
                continue
            try:
                self._emit_explicit_waiting_state(line, event_callback)
                self.handle_stream_output_line(line, event_callback)
            except GenerationInterrupted:
                _terminate_cli_process_tree(
                    process,
                    windows_job_handle=windows_job_handle,
                )
                reader.join(timeout=1.0)
                raise
            except Exception:
                logger.debug(
                    "[%s] Stream output event handling failed",
                    self.provider_name,
                    exc_info=True,
                )

        return_code = process.wait()
        stopped_windows_job = windows_job_handle if receipt is not None and _stop_windows_process_job(windows_job_handle) else None
        _close_windows_process_job(windows_job_handle)
        if receipt is not None:
            receipt.confirm_exit(process, stopped_windows_job=stopped_windows_job)
        return return_code, _decode_cli_output(b"".join(stdout_chunks)), ""

    def prepare_image_attachment(
        self, image_data: Dict[str, Any], cwd: Optional[Path] = None
    ) -> Optional[Tuple[str, Any]]:
        """
        Prepare image for CLI prompt injection

        Subclasses override this to handle image input (e.g., save base64 to
        a temp file and return "@filepath" to append to the prompt).

        Args:
            image_data: Image dict {data: base64 data URL, mimeType: str, name: str}
            cwd: Working directory for the CLI process. Used to save temp files
                 within the project scope when the CLI enforces path sandboxing.

        Returns:
            (prompt_suffix, cleanup_fn) or None if images are not supported.
            prompt_suffix is appended to the user prompt (e.g., " @/tmp/img.png").
            cleanup_fn is called after CLI execution to remove temp files.
        """
        return None

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """
        Get CLI arguments for MCP server configuration

        Each CLI tool has its own way to configure MCP servers:
        - Claude Code: --mcp-config JSON (command-line option)
        - Antigravity CLI: native plugin/settings files, no runtime CLI option
        - Codex CLI: ~/.codex/config.toml (settings file, no CLI option)

        Subclasses override this to provide CLI-specific MCP arguments.
        Default returns empty list (no CLI-level MCP support).

        Args:
            mcp_servers: Dict of server configs from AoiTalk config.yaml
                         Format: {name: {windows: {command, args}, linux: {command, args}, env: {...}}}

        Returns:
            List of additional CLI arguments for MCP support
        """
        if mcp_servers:
            logger.info(
                f"[{self.provider_name}] MCP servers configured in config.yaml, "
                f"but this CLI does not support runtime MCP arguments. "
                f"Configure MCP in the CLI's native settings file."
            )
        return []

    def is_available(self) -> bool:
        """
        Check if CLI is available

        Returns:
            True if CLI is available
        """
        try:
            cmd = self.get_cli_command("")
            bin_path = shutil.which(cmd[0]) or cmd[0]
            result = subprocess.run(
                [bin_path, "--version"],
                capture_output=True,
                env=self.get_subprocess_env(),
                timeout=5,
                creationflags=_windows_hidden_creationflags(),
            )
            return result.returncode == 0
        except Exception:
            return False
