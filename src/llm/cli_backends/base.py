"""
Abstract base class for CLI-based LLM backends

Provides common interface for Gemini CLI, Claude Code, Codex CLI, etc.
"""

import logging
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# コマンドライン引数の長さ上限（安全マージン込み）
# Windows: 約32,767文字だが余裕を持たせる
_MAX_ARG_LENGTH = 8000


class CLIBackendBase(ABC):
    """
    Abstract base class for CLI-based LLM backends

    Subclasses must implement:
    - get_cli_command(prompt): Return CLI command with prompt as argument
    - get_provider_name(): Return provider name for logging
    - parse_output(raw_output): Filter/transform CLI output
    """

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
            List of command parts (e.g., ["gemini", "--yolo", "-p", prompt])
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """
        Get provider name for logging

        Returns:
            Provider name (e.g., "Gemini CLI")
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

    def parse_tool_calls(self, cli_output: str) -> List[Dict[str, Any]]:
        """
        Parse tool calls from CLI output

        Subclasses can override to detect and parse tool call requests.
        Default returns empty list (no tool calls).

        Args:
            cli_output: Parsed CLI output text

        Returns:
            List of tool call dicts, empty if no tool calls detected
        """
        return []

    def execute_prompt(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: int = 300,
        extra_args: Optional[List[str]] = None,
        system_context: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Execute prompt via CLI

        Args:
            prompt: Prompt to execute (user message or full prompt)
            cwd: Working directory
            timeout: Timeout in seconds
            extra_args: Additional CLI arguments (e.g., MCP config)
            system_context: System context to pass via stdin (instructions, history, tools).
                           When provided, prompt is passed via -p and system_context via stdin.
                           Gemini CLI appends -p to stdin, so the user message comes last.

        Returns:
            (success: bool, output: str)
        """
        if system_context:
            # system_context → stdin, prompt → -p
            # Gemini CLI: "-p is appended to input on stdin"
            if len(prompt) > _MAX_ARG_LENGTH:
                # User message too long for -p, concatenate into stdin
                cmd = self.get_cli_command("")
                stdin_input = f"{system_context}\n\n{prompt}"
                logger.info(f"[{self.provider_name}] Using stdin for system_context + prompt")
            else:
                cmd = self.get_cli_command(prompt)
                stdin_input = system_context
                logger.info(f"[{self.provider_name}] Using stdin for system_context, -p for user prompt")
        elif len(prompt) > _MAX_ARG_LENGTH:
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

        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    cmd,
                    input=stdin_input,
                    cwd=str(cwd) if cwd else None,
                    text=True,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    timeout=timeout,
                )

                if result.returncode == 0:
                    output = self.parse_output(result.stdout)
                    logger.info(f"[{self.provider_name}] Execution successful: {len(output)} chars")
                    return True, output

                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                logger.warning(
                    f"[{self.provider_name}] Attempt {attempt+1}/{max_retries} "
                    f"failed (exit code {result.returncode})"
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

                error_msg = f"CLI failed (exit code {result.returncode})"
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

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """
        Get CLI arguments for MCP server configuration

        Each CLI tool has its own way to configure MCP servers:
        - Claude Code: --mcp-config JSON (command-line option)
        - Gemini CLI: ~/.gemini/settings.json (settings file, no CLI option)
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
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False
