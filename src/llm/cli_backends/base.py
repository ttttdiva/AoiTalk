"""
Abstract base class for CLI-based LLM backends

Provides common interface for Antigravity CLI, Claude Code, Codex CLI, etc.
"""

import logging
import locale
import os
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
from typing import List, Dict, Any, Optional, Tuple, Callable

from ..generation_cancellation import (
    GenerationCancelled,
    GenerationInterrupted,
    get_current_generation_cancellation,
    raise_if_generation_interrupted,
)
from ...utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)

# コマンドライン引数の長さ上限（安全マージン込み）
# Windows: 約32,767文字だが余裕を持たせる
_MAX_ARG_LENGTH = 8000
CLIEventCallback = Callable[[str, Dict[str, Any]], Any]


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
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
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


def _terminate_cli_process_tree(
    process: subprocess.Popen,
    *,
    windows_job_handle: int | None = None,
) -> None:
    """Stop the CLI wrapper and any child process it launched."""

    if os.name == "nt":
        _close_windows_process_job(windows_job_handle)
        try:
            if process.poll() is None:
                taskkill_result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2.0,
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
        logger.info(f"[{self.provider_name}] Backend initialized")

    def get_session_capabilities(self) -> CLISessionCapabilities:
        """Return the native session contract implemented by this backend."""
        return CLISessionCapabilities()

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
        return build_aoitalk_subprocess_env()

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
        self._last_usage = None
        self._usage_keys.clear()
        self._usage_invocation_id += 1
        action = str(native_session_action or "stateless").strip().lower()
        if action not in {"stateless", "start", "resume"}:
            action = "stateless"
        # Resolve the scope before any provider capability probe.  Codex,
        # Claude, and Grok discover native-session support by running their
        # host CLI ``--help`` command; that probe is itself forbidden while a
        # trusted repository scope is active.
        active_scope = self._active_run_scope()
        capabilities = (
            self.get_session_capabilities()
            if active_scope is None and action in {"start", "resume"}
            else CLISessionCapabilities()
        )
        self._active_native_session_action = action
        self._active_native_session_id = (
            str(native_session_id).strip() if native_session_id else None
        )
        self._active_native_session_ephemeral = ephemeral
        self._last_native_session_id = None
        self._last_native_resume_failure = False
        if (
            action == "start"
            and capabilities.native_sessions
            and capabilities.supports_explicit_session_id
            and not self._active_native_session_id
        ):
            self._active_native_session_id = self.create_native_session_id()
        if ephemeral is None and action in {"start", "resume"} and capabilities.native_sessions:
            self._active_native_session_ephemeral = False
        if active_scope is not None and action in {"start", "resume"}:
            return (
                False,
                f"{self.provider_name} scoped execution denied: native CLI sessions "
                "are unavailable in the WSL2/bubblewrap lane",
            )
        direct_prompt_max_length = getattr(
            self,
            "direct_prompt_max_length",
            _MAX_ARG_LENGTH,
        )
        prompt_stdin_supported = getattr(self, "prompt_stdin_supported", True)

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

        logger.info(f"[{self.provider_name}] Executing: {cmd[0]}")
        logger.debug(f"[{self.provider_name}] Prompt length: {len(prompt)} chars")
        subprocess_env = self.get_subprocess_env()

        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                stdin_input_bytes = (
                    stdin_input.encode("utf-8") if stdin_input is not None else None
                )
                cancellation_handle = get_current_generation_cancellation()
                if event_callback or cancellation_handle is not None:
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
                    )
                    returncode = result.returncode
                    stdout_text = _decode_cli_output(result.stdout)
                    stderr_text = _decode_cli_output(result.stderr)

                if returncode == 0:
                    if action in {"start", "resume"} and capabilities.native_sessions:
                        extracted_session_id = self.extract_native_session_id(stdout_text)
                        self._last_native_session_id = (
                            extracted_session_id
                            or self._active_native_session_id
                        )
                    output = self.parse_output(stdout_text)
                    logger.info(f"[{self.provider_name}] Execution successful: {len(output)} chars")
                    return True, output

                stderr = stderr_text.strip()
                stdout = stdout_text.strip()
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

                return False, error_msg

            except FileNotFoundError:
                error_msg = f"CLI not found: {cmd[0]}"
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except subprocess.TimeoutExpired:
                error_msg = f"CLI execution timed out ({timeout}s)"
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except GenerationCancelled:
                raise
            except GenerationInterrupted:
                raise
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
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

        try:
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
            if event_callback or cancellation_handle is not None:
                returncode, stdout_text, stderr_text = self._run_scoped_streaming_process(
                    backend,
                    scope,
                    command,
                    stdin_input_bytes=stdin_input,
                    cwd=safe_cwd,
                    env=env,
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
                    popen_kwargs={
                        "stdin": (
                            subprocess.PIPE
                            if stdin_input is not None
                            else subprocess.DEVNULL
                        ),
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": False,
                    },
                )
                try:
                    stdout, stderr = process.communicate(
                        input=stdin_input,
                        timeout=timeout,
                    )
                except subprocess.TimeoutExpired:
                    _terminate_cli_process_tree(process)
                    raise
                returncode = process.returncode
                stdout_text = _decode_cli_output(stdout)
                stderr_text = _decode_cli_output(stderr)
        except FileNotFoundError:
            return False, f"CLI not found inside scoped WSL backend: {cmd[0] if cmd else provider}"
        except subprocess.TimeoutExpired:
            error_msg = f"CLI execution timed out ({timeout}s)"
            logger.error("[%s] %s", provider, error_msg)
            return False, error_msg
        except GenerationCancelled:
            raise
        except GenerationInterrupted:
            raise
        except Exception as exc:
            logger.error("[%s] Scoped CLI execution failed: %s", provider, exc)
            return False, f"{provider} scoped execution denied: {exc}"

        if returncode == 0:
            if action in {"start", "resume"} and capabilities.native_sessions:
                extracted_session_id = self.extract_native_session_id(stdout_text)
                self._last_native_session_id = (
                    extracted_session_id or self._active_native_session_id
                )
            output = self.parse_output(stdout_text)
            logger.info("[%s] Scoped execution successful: %s chars", provider, len(output))
            return True, output

        stderr = stderr_text.strip()
        stdout = stdout_text.strip()
        parsed_error = self.parse_error_output(stdout, stderr, returncode)
        if parsed_error:
            error_msg = parsed_error
        else:
            error_msg = f"CLI failed (exit code {returncode})"
            if stderr:
                error_msg += f"\nSTDERR: {stderr}"
            if stdout:
                error_msg += f"\nSTDOUT: {stdout}"
        if action == "resume":
            self._last_native_resume_failure = self.is_native_session_resume_failure(error_msg)
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
        timeout: Optional[int],
        event_callback: CLIEventCallback,
    ) -> tuple[int, str, str]:
        """Stream a backend-owned WSL process without host ``Popen``."""

        stdout_queue: queue.Queue[bytes | None] = queue.Queue()
        stdout_chunks: list[bytes] = []
        process = backend.spawn(
            scope,
            command,
            cwd=cwd,
            shell="bash",
            timeout=timeout,
            env=env,
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
                _terminate_cli_process_tree(process)
                reader.join(timeout=1.0)
                raise GenerationCancelled("CLI generation cancelled")
            if (
                cancellation_handle is not None
                and cancellation_handle.interrupt_requested.is_set()
            ):
                _terminate_cli_process_tree(process)
                reader.join(timeout=1.0)
                raise_if_generation_interrupted()
            if deadline is None:
                queue_timeout = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
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
            stdout_chunks.append(chunk)
            line = _decode_cli_output(chunk).rstrip("\r\n")
            if not line:
                continue
            try:
                self.handle_stream_output_line(line, event_callback)
            except GenerationInterrupted:
                _terminate_cli_process_tree(process)
                reader.join(timeout=1.0)
                raise
            except Exception:
                logger.debug(
                    "[%s] Scoped stream event handling failed",
                    self.provider_name,
                    exc_info=True,
                )

        return_code = process.wait()
        return return_code, _decode_cli_output(b"".join(stdout_chunks)), ""

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

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
            env=env,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        windows_job_handle = _create_windows_process_job(process)

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
        _close_windows_process_job(windows_job_handle)
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
            )
            return result.returncode == 0
        except Exception:
            return False
