"""
OpenAI Codex CLI backend implementation.

Usage: codex exec [--model MODEL] --json

MCP: Codex CLI reads MCP config from ~/.codex/config.toml
     No command-line option is available.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CLIBackendBase

logger = logging.getLogger(__name__)

_USAGE_LIMIT_PREFIX = "You've hit your usage limit for "

_CODEX_CHAT_ADAPTER_PREAMBLE = """\
You are being used as a plain text-generation backend inside the AoiTalk chat app.
Do not act as a coding agent unless the user explicitly asks for code or repository work.
Do not inspect files, edit files, create branches, mention AGENTS.md/CLAUDE.md, or describe Codex operating policy.
Treat the following content as chat instructions and conversation context for the assistant response.
Return only the final assistant message for the current user request.
"""


class CodexCLIBackend(CLIBackendBase):
    """Codex CLI backend implementation."""

    def __init__(
        self,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ):
        self._model = model
        self._reasoning_effort = reasoning_effort
        super().__init__()

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build a Codex CLI command."""
        bin_path = os.getenv("CODEX_BIN", "codex")
        cmd = [
            bin_path,
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--ignore-rules",
        ]

        model = self._model or os.getenv("CODEX_MODEL")
        if model:
            cmd.extend(["--model", model])

        reasoning_effort = self._reasoning_effort or os.getenv("CODEX_REASONING_EFFORT")
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        sandbox = os.getenv("CODEX_SANDBOX", "read-only").strip()
        if sandbox:
            cmd.extend(["--sandbox", sandbox])

        if prompt:
            cmd.append(prompt)

        return cmd

    def get_provider_name(self) -> str:
        return "Codex CLI"

    def parse_output(self, raw_output: str) -> str:
        """Extract the final assistant message from Codex JSONL output."""
        agent_messages: list[str] = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                agent_messages.append(text.strip())

        if agent_messages:
            return agent_messages[-1]

        return super().parse_output(raw_output)

    def parse_error_output(self, stdout: str, stderr: str, exit_code: int) -> Optional[str]:
        """Extract Codex JSONL failure messages into a concise chat error."""
        error_message = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "error" and isinstance(event.get("message"), str):
                error_message = event["message"].strip()
                continue

            if event.get("type") == "turn.failed":
                error = event.get("error") or {}
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    error_message = message.strip()

        if not error_message:
            return None

        if error_message.startswith(_USAGE_LIMIT_PREFIX):
            return self._format_usage_limit_error(error_message)

        return f"Codex CLI の実行に失敗しました: {error_message}"

    def _format_usage_limit_error(self, message: str) -> str:
        tail = message[len(_USAGE_LIMIT_PREFIX):]
        model, _, rest = tail.partition(". ")
        retry_hint = rest.strip()
        if retry_hint.startswith("Switch to another model now, or try again at "):
            retry_at = retry_hint.removeprefix(
                "Switch to another model now, or try again at "
            ).rstrip(".")
            return (
                f"Codex CLI の利用上限に達しました（{model}）。"
                f"別の Codex モデルへ切り替えるか、{retry_at} 以降に再試行してください。"
            )

        return f"Codex CLI の利用上限に達しました（{model}）。別のモデルへ切り替えてください。"

    def execute_prompt(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: int = 300,
        extra_args: Optional[List[str]] = None,
        system_context: Optional[str] = None,
    ):
        """Codex accepts one initial instruction stream, so pass it via stdin."""
        combined_prompt = prompt
        if system_context:
            combined_prompt = (
                f"{system_context}\n\nUser request:\n{prompt}" if prompt else system_context
            )
        combined_prompt = f"{_CODEX_CHAT_ADAPTER_PREAMBLE}\n\n{combined_prompt}"

        return super().execute_prompt(
            "",
            cwd=cwd,
            timeout=timeout,
            extra_args=extra_args,
            system_context=combined_prompt,
        )

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Codex CLI does not support runtime MCP arguments."""
        if mcp_servers:
            logger.info(
                "[Codex CLI] %s MCP server(s) in config.yaml. "
                "Codex CLI requires MCP to be configured in ~/.codex/config.toml",
                len(mcp_servers),
            )
        return []
