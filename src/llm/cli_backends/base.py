"""
Abstract base class for CLI-based LLM backends

Provides common interface for Antigravity CLI, Claude Code, Codex CLI, etc.
"""

import logging
import locale
import os
import queue
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable

logger = logging.getLogger(__name__)

# コマンドライン引数の長さ上限（安全マージン込み）
# Windows: 約32,767文字だが余裕を持たせる
_MAX_ARG_LENGTH = 8000
CLIEventCallback = Callable[[str, Dict[str, Any]], Any]


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

    def __init__(self):
        """Initialize CLI backend"""
        self.provider_name = self.get_provider_name()
        logger.info(f"[{self.provider_name}] Backend initialized")

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

    def get_subprocess_env(self) -> Optional[Dict[str, str]]:
        """Return environment overrides for CLI subprocesses."""
        return None

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
        timeout: int = 300,
        extra_args: Optional[List[str]] = None,
        system_context: Optional[str] = None,
        event_callback: Optional[CLIEventCallback] = None,
    ) -> Tuple[bool, str]:
        """
        Execute prompt via CLI

        Args:
            prompt: Prompt to execute (user message or full prompt)
            cwd: Working directory
            timeout: Timeout in seconds
            extra_args: Additional CLI arguments (e.g., MCP config)
            system_context: System context to pass via stdin (instructions, history, tools).
                           When provided, prompt is passed via CLI argument and
                           system_context via stdin for backends that support it.

        Returns:
            (success: bool, output: str)
        """
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

        # MCP config等の追加引数
        if extra_args:
            cmd.extend(extra_args)

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
                if event_callback:
                    returncode, stdout_text, stderr_text = self._run_streaming_process(
                        cmd,
                        stdin_input_bytes=stdin_input_bytes,
                        cwd=cwd,
                        env=subprocess_env,
                        timeout=timeout,
                        event_callback=event_callback,
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

                return False, error_msg

            except FileNotFoundError:
                error_msg = f"CLI not found: {cmd[0]}"
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except subprocess.TimeoutExpired:
                error_msg = f"CLI execution timed out ({timeout}s)"
                logger.error(f"[{self.provider_name}] {error_msg}")
                return False, error_msg
            except Exception as e:
                error_msg = f"Unexpected error: {e}"
                logger.error(f"[{self.provider_name}] {error_msg}", exc_info=True)
                return False, error_msg

        return False, "Max retries exceeded"

    def _run_streaming_process(
        self,
        cmd: List[str],
        *,
        stdin_input_bytes: Optional[bytes],
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        timeout: int,
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

        deadline = time.monotonic() + timeout
        reader_done = False
        while not reader_done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                reader.join(timeout=1.0)
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                chunk = stdout_queue.get(timeout=min(0.1, max(remaining, 0.01)))
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
            except Exception:
                logger.debug(
                    "[%s] Stream output event handling failed",
                    self.provider_name,
                    exc_info=True,
                )

        return_code = process.wait(timeout=1)
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
