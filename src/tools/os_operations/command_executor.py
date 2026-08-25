"""
Command Executor for AoiTalk

Provides subprocess-based command execution with:
- Windows/Linux/Mac platform detection
- Timeout handling
- Output streaming
- Security restrictions (allowed paths, command blacklist)

Based on Open Interpreter's subprocess_language.py patterns.
"""

import logging
import os
import platform
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, List, Optional, Set

from ...utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)


SUPPORTED_SHELLS = ("auto", "cmd", "powershell", "bash")


# 停止時に terminate → kill へ切り替えるまでの猶予（秒）
TERMINATE_GRACE_SECONDS = 3.0


def build_process_group_kwargs() -> dict:
    """プロセスツリーごと停止できるよう独立グループで起動するための Popen 引数"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _wait_for_exit(process: subprocess.Popen, seconds: float) -> bool:
    """指定秒数だけプロセスの終了を待つ。終了したら True。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return process.poll() is not None


def _run_taskkill(pid: int, force: bool) -> None:
    """Windows でプロセスツリーを終了させる"""
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        subprocess.run(args, capture_output=True, timeout=10)
    except Exception:
        pass


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    """POSIX でプロセスグループ全体へシグナルを送る"""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except Exception:
        try:
            process.send_signal(sig)
        except Exception:
            pass


def terminate_process_tree(
    process: subprocess.Popen,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
) -> None:
    """terminate してから猶予を置き、残っていれば子孫ごと kill する。

    シェル経由で起動しているため、シェルだけを殺すと孫プロセス
    （実際のサーバやビルドプロセス）が残る。必ずツリー全体を対象にする。
    """
    if process.poll() is not None:
        return

    if os.name == "nt":
        # ``wsl.exe`` hosts a separate Linux PID namespace.  A graceful
        # ``taskkill`` can terminate only the host wrapper while allowing a
        # bwrap descendant to continue until the normal grace period elapses;
        # that would violate the file-scoped mutation boundary.  The backend
        # opts into an immediate tree kill and ``--die-with-parent`` cleans up
        # the namespace descendants after the wrapper exits.
        if getattr(process, "_aoitalk_wsl_bwrap", False):
            _run_taskkill(process.pid, force=True)
            if _wait_for_exit(process, 2.0):
                return
        _run_taskkill(process.pid, force=False)
        if _wait_for_exit(process, grace_seconds):
            return
        _run_taskkill(process.pid, force=True)
        if _wait_for_exit(process, 2.0):
            return
    else:
        import signal

        _signal_process_group(process, signal.SIGTERM)
        if _wait_for_exit(process, grace_seconds):
            return
        _signal_process_group(process, signal.SIGKILL)
        if _wait_for_exit(process, 2.0):
            return

    # 最後の手段
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=2)
    except Exception:
        pass

# PowerShell の出力を UTF-8 に固定するための前置きコマンド
_POWERSHELL_UTF8_PREFIX = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
)


def resolve_shell_name(shell: Optional[str]) -> str:
    """`auto` を実際のシェル名へ解決する。

    Windows は powershell、Unix 系は $SHELL（未設定なら bash）を使う。

    Args:
        shell: auto / cmd / powershell / bash（None は auto 扱い）

    Returns:
        str: 解決後のシェル名

    Raises:
        ValueError: サポート外のシェル名が指定された場合
    """
    name = (shell or "auto").strip().lower()
    if name not in SUPPORTED_SHELLS:
        raise ValueError(
            f"サポートされていないシェルです: {shell} "
            f"（利用可能: {', '.join(SUPPORTED_SHELLS)}）"
        )
    if name != "auto":
        return name

    if platform.system() == "Windows":
        return "powershell"

    env_shell = os.environ.get("SHELL", "")
    base = os.path.basename(env_shell) if env_shell else ""
    return base or "bash"


def build_shell_command(command: str, shell: Optional[str] = None) -> List[str]:
    """シェル名からサブプロセス起動用の引数リストを組み立てる。

    Args:
        command: 実行するコマンド文字列
        shell: auto / cmd / powershell / bash

    Returns:
        List[str]: subprocess へ渡す引数リスト

    Raises:
        ValueError: サポート外のシェル名が指定された場合
    """
    name = resolve_shell_name(shell)

    if name == "cmd":
        return ["cmd.exe", "/c", command]
    if name == "powershell":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _POWERSHELL_UTF8_PREFIX + command,
        ]
    if name == "bash":
        return ["bash", "-c", command]

    # $SHELL から解決した zsh / fish などはそのまま -c で起動する
    env_shell = os.environ.get("SHELL", "")
    executable = env_shell if env_shell and os.path.basename(env_shell) == name else name
    return [executable, "-c", command]


@dataclass
class CommandResult:
    """Result of a command execution"""
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False
    error_message: str = ""
    duration_seconds: float = 0.0


class CommandExecutor:
    """
    Shell command execution engine with Windows/Linux support.
    
    Features:
    - Platform-aware shell selection (cmd.exe on Windows, bash on Unix)
    - Timeout handling
    - Output streaming for long-running commands
    - Security restrictions (allowed paths, command blacklist)
    """
    
    # Dangerous commands that should be blocked
    DANGEROUS_PATTERNS: List[str] = [
        r"rm\s+-rf\s+/",           # rm -rf /
        r"rm\s+-rf\s+\*",          # rm -rf *
        r"del\s+/[sf]\s+",         # Windows del /s /f
        r"format\s+[a-zA-Z]:",     # Windows format drive
        r"mkfs\.",                 # Linux format
        r":(){.*};:",              # Fork bomb
        r">\s*/dev/sd",            # Overwrite disk
        r"dd\s+if=.*of=/dev/sd",   # dd to disk
    ]
    
    def __init__(
        self,
        allowed_paths: Optional[List[str]] = None,
        timeout: int = 120,
        enable_dangerous_check: bool = True,
    ):
        """
        Initialize the command executor.
        
        Args:
            allowed_paths: List of paths where commands can be executed.
                          If None, loads from AOITALK_ALLOWED_PATHS env var.
            timeout: Default timeout in seconds for command execution.
            enable_dangerous_check: Whether to block dangerous commands.
        """
        self.timeout = timeout
        self.enable_dangerous_check = enable_dangerous_check
        
        # Load allowed paths from environment if not specified
        if allowed_paths is None:
            env_paths = os.environ.get("AOITALK_ALLOWED_PATHS", "")
            if env_paths:
                self.allowed_paths = [p.strip() for p in env_paths.split(",") if p.strip()]
            else:
                # Default: allow current working directory and common safe paths
                self.allowed_paths = []
        else:
            self.allowed_paths = allowed_paths
            
        # Platform-specific shell configuration
        if platform.system() == "Windows":
            self.shell_cmd = ["cmd.exe", "/c"]
            self.shell_name = "cmd"
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            self.shell_cmd = [shell, "-c"]
            self.shell_name = os.path.basename(shell)
            
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]
        
    def _is_dangerous_command(self, command: str) -> bool:
        """Check if command matches any dangerous patterns."""
        if not self.enable_dangerous_check:
            return False
            
        for pattern in self._compiled_patterns:
            if pattern.search(command):
                return True
        return False
        
    def _validate_cwd(self, cwd: Optional[str], scope_override=None) -> Optional[Path]:
        """Validate and resolve the working directory."""
        try:
            from ...security.agent_run_scope import RunScopeViolation, get_current_run_scope
        except ImportError:  # pragma: no cover - defensive for stripped builds
            class RunScopeViolation(Exception):
                pass

            get_current_run_scope = lambda: None  # type: ignore[assignment]

        scope = scope_override if scope_override is not None else get_current_run_scope()
        if scope is not None:
            try:
                # ``None`` means the selected repository root, not the
                # process-global cwd.
                return scope.assert_command_cwd_allowed(cwd)
            except RunScopeViolation as exc:
                raise ValueError(str(exc)) from exc

        if cwd is None:
            return None
            
        cwd_path = Path(cwd).resolve()
        
        if not cwd_path.exists():
            raise ValueError(f"Directory does not exist: {cwd}")
            
        if not cwd_path.is_dir():
            raise ValueError(f"Path is not a directory: {cwd}")
            
        # Check if cwd is within allowed paths (if restrictions are set)
        if self.allowed_paths:
            allowed = False
            for allowed_path in self.allowed_paths:
                try:
                    cwd_path.relative_to(Path(allowed_path).resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise ValueError(
                    f"Directory is outside allowed paths: {cwd}. "
                    f"Allowed paths: {self.allowed_paths}"
                )
                
        return cwd_path

    def _run_scoped_shell_error(self, scope_override=None) -> Optional[str]:
        """Return the fail-closed error for arbitrary run-scoped shells."""

        try:
            from ...security.agent_run_scope import get_current_run_scope
        except ImportError:  # pragma: no cover - defensive for stripped builds
            return None
        scope = scope_override if scope_override is not None else get_current_run_scope()
        if scope is None:
            return None
        return (
            "run-scoped shell execution is disabled for streaming in the "
            "generic command executor: use the bounded foreground WSL2 "
            "file-scoped backend or agent_harness/Codex; cwd/command-text "
            "checks are not a mutation boundary"
        )

    def _get_scoped_backend(self, scope_override=None):
        """Resolve the concrete file-scoped backend for an active run."""

        try:
            from ...security.agent_run_scope import get_current_run_scope
            from ...security.wsl_bwrap_backend import (
                WslBwrapError,
                get_wsl_bwrap_backend,
            )
        except ImportError as exc:  # pragma: no cover - defensive for stripped builds
            return None, f"file-scoped command backend is unavailable: {exc}"

        scope = scope_override if scope_override is not None else get_current_run_scope()
        if scope is None:
            return None, None
        try:
            backend = get_wsl_bwrap_backend()
            if not getattr(backend, "file_scoped", False):
                return None, "configured command backend is not file-scoped"
            if not backend.is_available():
                return None, (
                    "file-scoped WSL2/bubblewrap backend is unavailable; "
                    "run-scoped shell execution was denied"
                )
            return (scope, backend), None
        except WslBwrapError as exc:
            return None, str(exc)

    @staticmethod
    def _from_scoped_backend_result(result) -> CommandResult:
        """Adapt ``SandboxResult`` without coupling the security module to tools."""

        return CommandResult(
            success=bool(result.success),
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            return_code=(
                int(result.return_code)
                if result.return_code is not None
                else 0
            ),
            timed_out=bool(result.timed_out),
            error_message=str(result.error_message or ""),
            duration_seconds=float(result.duration_seconds or 0.0),
        )
        
    def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        shell: Optional[str] = None
    ) -> CommandResult:
        """
        Execute a shell command and return the result.

        Args:
            command: The command to execute.
            cwd: Working directory for the command.
            timeout: Timeout in seconds (uses default if not specified).
            shell: シェル種別（auto / cmd / powershell / bash）。None は auto。

        Returns:
            CommandResult with stdout, stderr, return code, duration, and status.
        """
        if timeout is None:
            timeout = self.timeout

        # Validate cwd before command-text checks so an invalid/out-of-scope
        # working directory is never hidden behind a regex classification.
        try:
            cwd_path = self._validate_cwd(cwd)
        except ValueError as e:
            return CommandResult(success=False, error_message=str(e))

        # Security checks
        if self._is_dangerous_command(command):
            return CommandResult(
                success=False,
                error_message=f"Command blocked for safety: matches dangerous pattern"
            )

        scoped_backend, backend_error = self._get_scoped_backend()
        if backend_error:
            return CommandResult(success=False, error_message=backend_error)
        if scoped_backend is not None:
            scope, backend = scoped_backend
            try:
                result = backend.run(
                    scope,
                    command,
                    cwd=cwd_path,
                    shell=shell,
                    timeout=timeout,
                    env=build_aoitalk_subprocess_env(),
                )
            except Exception as exc:
                logger.error("Scoped backend execution failed: %s", exc, exc_info=True)
                return CommandResult(
                    success=False,
                    error_message=f"file-scoped command backend failed: {exc}",
                )
            return self._from_scoped_backend_result(result)

        try:
            full_cmd = build_shell_command(command, shell)
        except ValueError as e:
            return CommandResult(success=False, error_message=str(e))

        logger.info(f"Executing command: {command[:100]}..." if len(command) > 100 else f"Executing command: {command}")

        started_at = time.monotonic()
        try:
            # Set up environment
            env = build_aoitalk_subprocess_env()

            process = subprocess.Popen(
                full_cmd,
                cwd=str(cwd_path) if cwd_path else None,
                stdin=subprocess.DEVNULL,  # 入力待ちでハングさせない
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                encoding="utf-8",
                errors="replace",
                **build_process_group_kwargs()
            )

            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning(f"Command timed out after {timeout}s: {command}")
                # シェルの子孫まで含めて確実に停止させる
                terminate_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except Exception:
                    stdout, stderr = "", ""
                return CommandResult(
                    success=False,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    timed_out=True,
                    error_message=f"Command timed out after {timeout} seconds",
                    duration_seconds=time.monotonic() - started_at
                )

            return CommandResult(
                success=process.returncode == 0,
                stdout=stdout or "",
                stderr=stderr or "",
                return_code=process.returncode,
                duration_seconds=time.monotonic() - started_at
            )

        except FileNotFoundError as e:
            logger.error(f"Shell not found: {e}")
            return CommandResult(
                success=False,
                error_message=f"Shell not found: {full_cmd[0]}",
                duration_seconds=time.monotonic() - started_at
            )

        except Exception as e:
            logger.error(f"Unexpected error executing command: {e}", exc_info=True)
            return CommandResult(
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                duration_seconds=time.monotonic() - started_at
            )
            
    def execute_streaming(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        shell: Optional[str] = None,
    ) -> Generator[str, None, CommandResult]:
        """Capture the active run scope before returning a lazy generator."""

        try:
            from ...security.agent_run_scope import get_current_run_scope
        except ImportError:  # pragma: no cover - defensive for stripped builds
            scope = None
        else:
            scope = get_current_run_scope()
        return self._execute_streaming_impl(
            command,
            cwd=cwd,
            timeout=timeout,
            shell=shell,
            scope_override=scope,
        )

    def _execute_streaming_impl(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        shell: Optional[str] = None,
        *,
        scope_override=None,
    ) -> Generator[str, None, CommandResult]:
        """
        Execute a command and yield output lines as they arrive.
        
        Args:
            command: The command to execute.
            cwd: Working directory for the command.
            timeout: Timeout in seconds.
            
        Yields:
            Output lines as they are produced.
            
        Returns:
            Final CommandResult after execution completes.
        """
        if timeout is None:
            timeout = self.timeout
            
        try:
            cwd_path = self._validate_cwd(cwd, scope_override=scope_override)
        except ValueError as e:
            return CommandResult(success=False, error_message=str(e))

        # Security checks
        if self._is_dangerous_command(command):
            return CommandResult(
                success=False,
                error_message=f"Command blocked for safety: matches dangerous pattern"
            )

        scoped_backend, backend_error = self._get_scoped_backend(
            scope_override=scope_override
        )
        if backend_error:
            return CommandResult(success=False, error_message=backend_error)

        if scoped_backend is not None:
            scope, backend = scoped_backend
            return (yield from self._execute_streaming_with_backend(
                backend,
                scope,
                command,
                cwd=cwd_path,
                timeout=timeout,
                shell=shell,
            ))
            
        logger.info(f"Executing (streaming): {command[:50]}...")
        
        try:
            full_cmd = build_shell_command(command, shell)
        except ValueError as exc:
            return CommandResult(success=False, error_message=str(exc))

        try:
            env = build_aoitalk_subprocess_env()
            
            process = subprocess.Popen(
                full_cmd,
                cwd=str(cwd_path) if cwd_path else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                env=env,
                encoding="utf-8",
                errors="replace",
                **build_process_group_kwargs(),
            )
            
            output_queue: queue.Queue = queue.Queue()
            stdout_lines: List[str] = []
            stderr_lines: List[str] = []
            
            def read_stream(stream, is_stderr: bool):
                try:
                    for line in iter(stream.readline, ""):
                        output_queue.put((line, is_stderr))
                        if is_stderr:
                            stderr_lines.append(line)
                        else:
                            stdout_lines.append(line)
                except ValueError:
                    pass  # Stream closed
                finally:
                    output_queue.put((None, is_stderr))  # Signal done
                    
            # Start reader threads
            stdout_thread = threading.Thread(
                target=read_stream, args=(process.stdout, False), daemon=True
            )
            stderr_thread = threading.Thread(
                target=read_stream, args=(process.stderr, True), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()
            
            # Read output with timeout
            start_time = time.time()
            streams_done = 0
            
            while streams_done < 2:
                try:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        terminate_process_tree(process)
                        try:
                            process.communicate(timeout=5)
                        except Exception:
                            pass
                        return CommandResult(
                            success=False,
                            stdout="".join(stdout_lines),
                            stderr="".join(stderr_lines),
                            timed_out=True,
                            return_code=(
                                process.returncode
                                if process.returncode is not None
                                else -1
                            ),
                            error_message=f"Command timed out after {timeout} seconds",
                            duration_seconds=time.time() - start_time,
                        )
                        
                    line, is_stderr = output_queue.get(timeout=min(0.5, remaining))
                    if line is None:
                        streams_done += 1
                    else:
                        yield line
                        
                except queue.Empty:
                    continue
                    
            process.wait()
            
            return CommandResult(
                success=process.returncode == 0,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                return_code=process.returncode
            )
            
        except Exception as e:
            logger.error(f"Unexpected error in streaming execution: {e}", exc_info=True)
            return CommandResult(
                success=False,
                error_message=f"Unexpected error: {str(e)}"
            )

    def _execute_streaming_with_backend(
        self,
        backend,
        scope,
        command: str,
        *,
        cwd: Optional[Path],
        timeout: Optional[int],
        shell: Optional[str],
    ) -> Generator[str, None, CommandResult]:
        """Stream one command from the file-scoped WSL backend.

        ``WslBwrapBackend.spawn`` returns the host-side ``wsl.exe`` process.
        The bubblewrap wrapper is launched with ``--die-with-parent``; this
        method additionally terminates the host process tree on timeout or
        generator close so no WSL descendants survive a bounded run.
        """

        started_at = time.monotonic()
        process = None
        output_queue: queue.Queue = queue.Queue()
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        streams_done = 0

        def read_stream(stream, is_stderr: bool) -> None:
            try:
                while True:
                    line = stream.readline()
                    if line in ("", b""):
                        break
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    output_queue.put((line, is_stderr))
                    if is_stderr:
                        stderr_lines.append(line)
                    else:
                        stdout_lines.append(line)
            except (ValueError, OSError):
                pass
            finally:
                output_queue.put((None, is_stderr))

        try:
            process = backend.spawn(
                scope,
                command,
                cwd=cwd,
                shell=shell,
                timeout=timeout,
                env=build_aoitalk_subprocess_env(),
                popen_kwargs=build_process_group_kwargs(),
            )
            stdout_thread = threading.Thread(
                target=read_stream,
                args=(process.stdout, False),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=read_stream,
                args=(process.stderr, True),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            effective_timeout = self.timeout if timeout is None else max(float(timeout), 0.01)
            deadline = time.monotonic() + effective_timeout
            while streams_done < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate_process_tree(process)
                    try:
                        process.communicate(timeout=5)
                    except Exception:
                        pass
                    return CommandResult(
                        success=False,
                        stdout="".join(stdout_lines),
                        stderr="".join(stderr_lines),
                        return_code=(
                            process.returncode
                            if process.returncode is not None
                            else -1
                        ),
                        timed_out=True,
                        error_message=f"Command timed out after {effective_timeout:g} seconds",
                        duration_seconds=time.monotonic() - started_at,
                    )
                try:
                    line, is_stderr = output_queue.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    streams_done += 1
                else:
                    yield line

            process.wait(timeout=5)
            return CommandResult(
                success=process.returncode == 0,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                return_code=process.returncode,
                duration_seconds=time.monotonic() - started_at,
                error_message=("".join(stderr_lines)).strip()
                if process.returncode
                else "",
            )
        except GeneratorExit:
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            raise
        except Exception as exc:
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            logger.error("Scoped streaming execution failed: %s", exc, exc_info=True)
            return CommandResult(
                success=False,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                return_code=(
                    process.returncode
                    if process is not None and process.returncode is not None
                    else -1
                ),
                error_message=f"file-scoped streaming backend failed: {exc}",
                duration_seconds=time.monotonic() - started_at,
            )


# Global instance
_executor: Optional[CommandExecutor] = None


def get_command_executor() -> CommandExecutor:
    """Get or create the global CommandExecutor instance."""
    global _executor
    if _executor is None:
        timeout = int(os.environ.get("AOITALK_COMMAND_TIMEOUT", "120"))
        _executor = CommandExecutor(timeout=timeout)
    return _executor
