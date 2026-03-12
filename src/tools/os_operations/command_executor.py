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

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of a command execution"""
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False
    error_message: str = ""


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
        enable_dangerous_check: bool = True
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
        
    def _validate_cwd(self, cwd: Optional[str]) -> Optional[Path]:
        """Validate and resolve the working directory."""
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
        
    def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """
        Execute a shell command and return the result.
        
        Args:
            command: The command to execute.
            cwd: Working directory for the command.
            timeout: Timeout in seconds (uses default if not specified).
            
        Returns:
            CommandResult with stdout, stderr, return code, and status.
        """
        if timeout is None:
            timeout = self.timeout
            
        # Security checks
        if self._is_dangerous_command(command):
            return CommandResult(
                success=False,
                error_message=f"Command blocked for safety: matches dangerous pattern"
            )
            
        try:
            cwd_path = self._validate_cwd(cwd)
        except ValueError as e:
            return CommandResult(success=False, error_message=str(e))
            
        logger.info(f"Executing command: {command[:100]}..." if len(command) > 100 else f"Executing command: {command}")
        
        try:
            # Build the full command
            if platform.system() == "Windows":
                # On Windows, pass command directly to cmd.exe /c
                full_cmd = self.shell_cmd + [command]
            else:
                # On Unix, pass as a single string to shell -c
                full_cmd = self.shell_cmd + [command]
                
            # Set up environment
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            result = subprocess.run(
                full_cmd,
                cwd=str(cwd_path) if cwd_path else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                encoding="utf-8",
                errors="replace"
            )
            
            return CommandResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode
            )
            
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out after {timeout}s: {command}")
            return CommandResult(
                success=False,
                timed_out=True,
                error_message=f"Command timed out after {timeout} seconds"
            )
            
        except FileNotFoundError as e:
            logger.error(f"Shell not found: {e}")
            return CommandResult(
                success=False,
                error_message=f"Shell not found: {self.shell_cmd[0]}"
            )
            
        except Exception as e:
            logger.error(f"Unexpected error executing command: {e}", exc_info=True)
            return CommandResult(
                success=False,
                error_message=f"Unexpected error: {str(e)}"
            )
            
    def execute_streaming(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None
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
            
        # Security checks
        if self._is_dangerous_command(command):
            return CommandResult(
                success=False,
                error_message=f"Command blocked for safety: matches dangerous pattern"
            )
            
        try:
            cwd_path = self._validate_cwd(cwd)
        except ValueError as e:
            return CommandResult(success=False, error_message=str(e))
            
        logger.info(f"Executing (streaming): {command[:50]}...")
        
        try:
            if platform.system() == "Windows":
                full_cmd = self.shell_cmd + [command]
            else:
                full_cmd = self.shell_cmd + [command]
                
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            process = subprocess.Popen(
                full_cmd,
                cwd=str(cwd_path) if cwd_path else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                env=env,
                encoding="utf-8",
                errors="replace"
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
                        process.kill()
                        return CommandResult(
                            success=False,
                            stdout="".join(stdout_lines),
                            stderr="".join(stderr_lines),
                            timed_out=True,
                            error_message=f"Command timed out after {timeout} seconds"
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


# Global instance
_executor: Optional[CommandExecutor] = None


def get_command_executor() -> CommandExecutor:
    """Get or create the global CommandExecutor instance."""
    global _executor
    if _executor is None:
        timeout = int(os.environ.get("AOITALK_COMMAND_TIMEOUT", "120"))
        _executor = CommandExecutor(timeout=timeout)
    return _executor
