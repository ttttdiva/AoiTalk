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

from ..generation_policy import (
    GenerationProfile,
    PermissionPolicy,
    get_current_generation_policy,
)
from .base import CLIBackendBase, CLIEventCallback

logger = logging.getLogger(__name__)

_USAGE_LIMIT_PREFIX = "You've hit your usage limit for "

_CODEX_ADAPTER_PREAMBLE = """\
You are the model backend inside the AoiTalk app.
Use the AoiTalk tools listed in the system context through the documented
[TOOL_CALL: name(args...)] format whenever the task requires file access, web
search, project DB updates, or other external actions that AoiTalk exposes.
Treat tool results as the only proof that external work happened, and continue
requesting the next needed tool call when a result reveals required follow-up
work. Return the final user-facing assistant message when the available tool
results are enough to answer accurately.
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
        policy = get_current_generation_policy()
        bin_path = os.getenv("CODEX_BIN", "codex")
        cmd = [
            bin_path,
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
        ]
        if policy.profile == GenerationProfile.CHAT:
            cmd.append("--ignore-rules")

        model = self._model or os.getenv("CODEX_MODEL")
        if model:
            cmd.extend(["--model", model])

        reasoning_effort = self._reasoning_effort or os.getenv("CODEX_REASONING_EFFORT")
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        approval_policy = os.getenv("CODEX_APPROVAL_POLICY")
        if (
            approval_policy is None
            and policy.permission_policy == PermissionPolicy.AUTO_APPROVE
        ):
            approval_policy = "never"
        if approval_policy:
            cmd.extend(["-c", f'approval_policy="{approval_policy.strip()}"'])

        sandbox = os.getenv("CODEX_SANDBOX")
        if sandbox is None:
            sandbox = (
                "workspace-write"
                if policy.permission_policy == PermissionPolicy.AUTO_APPROVE
                else "read-only"
            )
        sandbox = sandbox.strip()
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

    def handle_stream_output_line(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        """Convert Codex JSONL events into AoiTalk stream progress events."""
        stripped = line.strip()
        if not stripped.startswith("{"):
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return

        event_type = str(event.get("type") or event.get("method") or "")
        if event_type == "turn.started":
            event_callback(
                "status_update",
                {
                    "status": "codex_turn_started",
                    "message": "Codex CLI turn started",
                },
            )
            return
        if event_type == "turn.completed":
            event_callback(
                "status_update",
                {
                    "status": "codex_turn_completed",
                    "message": "Codex CLI turn completed",
                },
            )
            return
        if event_type == "error":
            message = event.get("message")
            event_callback(
                "status_update",
                {
                    "status": "codex_error",
                    "message": str(message or "Codex CLI error"),
                },
            )
            return

        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "")
        label = self._stream_item_label(item)
        if event_type == "item.started":
            if self._is_tool_like_item(item):
                event_callback(
                    "tool_start",
                    {
                        "tool": label,
                        "tool_args": {},
                        "message": f"Codex CLI started {label}",
                    },
                )
            elif item_type:
                event_callback(
                    "status_update",
                    {
                        "status": f"codex_{item_type}_started",
                        "message": f"Codex CLI started {label}",
                    },
                )
            return
        if event_type == "item.completed":
            if self._is_tool_like_item(item):
                event_callback(
                    "tool_end",
                    {
                        "tool": label,
                        "tool_args": {},
                        "message": f"Codex CLI completed {label}",
                        "tool_result": {
                            "tool": label,
                            "arguments": {},
                            "output": self._stream_item_output(item),
                        },
                    },
                )
            elif item_type and item_type != "agent_message":
                event_callback(
                    "status_update",
                    {
                        "status": f"codex_{item_type}_completed",
                        "message": f"Codex CLI completed {label}",
                    },
                )

    def _stream_item_label(self, item: dict[str, Any]) -> str:
        for key in ("name", "command", "tool", "title", "type"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "item"

    def _stream_item_output(self, item: dict[str, Any]) -> str:
        for key in ("output", "text", "result"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _is_tool_like_item(self, item: dict[str, Any]) -> bool:
        item_type = str(item.get("type") or "").lower()
        return item_type in {
            "command",
            "command_execution",
            "function_call",
            "tool_call",
            "shell",
        }

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
        event_callback: Optional[CLIEventCallback] = None,
    ):
        """Codex accepts one initial instruction stream, so pass it via stdin."""
        policy = get_current_generation_policy()
        combined_prompt = prompt
        if system_context:
            combined_prompt = (
                f"{system_context}\n\nUser request:\n{prompt}" if prompt else system_context
            )
        combined_prompt = f"{_CODEX_ADAPTER_PREAMBLE}\n\n{combined_prompt}"

        return super().execute_prompt(
            "",
            cwd=cwd,
            timeout=timeout,
            extra_args=extra_args,
            system_context=combined_prompt,
            event_callback=event_callback,
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
