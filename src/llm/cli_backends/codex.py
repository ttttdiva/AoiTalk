"""
OpenAI Codex CLI backend implementation.

Usage: codex exec [--model MODEL] --json

MCP: Codex CLI reads MCP config from ~/.codex/config.toml
     No command-line option is available.
"""

import base64
import json
import logging
import os
import tempfile
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
        self._image_paths: list[Path] = []
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

        for image_path in self._image_paths:
            cmd.extend(["--image", str(image_path)])

        reasoning_effort = self._reasoning_effort or os.getenv("CODEX_REASONING_EFFORT")
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        approval_policy = (
            "never"
            if policy.permission_policy == PermissionPolicy.AUTO_APPROVE
            else os.getenv("CODEX_APPROVAL_POLICY")
        )
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

    def prepare_image_attachment(
        self, image_data: Dict[str, Any], cwd: Optional[Path] = None
    ):
        """Pass an image through Codex CLI's documented --image option."""
        data_url = str(image_data.get("data") or "")
        if not data_url:
            return None
        try:
            header, encoded = data_url.split(",", 1) if data_url.startswith("data:") else ("", data_url)
            mime_type = str(image_data.get("mimeType") or header.split(";", 1)[0].removeprefix("data:") or "image/png")
            suffix = {
                "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/webp": ".webp", "image/gif": ".gif",
            }.get(mime_type, ".png")
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
                fp.write(base64.b64decode(encoded))
                path = Path(fp.name)
            self._image_paths.append(path)

            def cleanup() -> None:
                for image_path in self._image_paths:
                    image_path.unlink(missing_ok=True)
                self._image_paths.clear()

            return ("", cleanup)
        except Exception as exc:
            logger.warning("[Codex CLI] Image preparation failed: %s", exc)
            return None

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
            if event.get("type") == "turn.completed":
                self.set_last_usage(event.get("usage"))
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
        operation_id = str(item.get("id") or item.get("call_id") or "").strip()
        if event_type == "item.started":
            if self._is_tool_like_item(item):
                tool_name, tool_args = self._stream_tool_context(item)
                payload = {
                    "tool": tool_name,
                    "tool_args": tool_args,
                    "message": f"Codex CLI started {tool_name}",
                }
                if operation_id:
                    payload["operation_id"] = operation_id
                event_callback(
                    "tool_start",
                    payload,
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
                tool_name, tool_args = self._stream_tool_context(item)
                tool_result = {
                    "tool": tool_name,
                    "arguments": tool_args,
                    "output": self._stream_item_output(item),
                }
                for source_key, target_key in (
                    ("exit_code", "exit_code"),
                    ("stderr", "stderr"),
                    ("error", "error"),
                ):
                    value = item.get(source_key)
                    if value is not None and value != "":
                        tool_result[target_key] = value
                exit_code = item.get("exit_code")
                failed_status = str(item.get("status") or "").lower() in {
                    "failed",
                    "error",
                }
                nonzero_exit = exit_code is not None and str(exit_code).strip() not in {
                    "0",
                    "0.0",
                }
                if (failed_status or nonzero_exit) and not tool_result.get("error"):
                    tool_result["error"] = (
                        f"コマンドが終了コード {exit_code} で失敗しました"
                        if exit_code is not None
                        else "コマンドの実行に失敗しました"
                    )
                payload = {
                    "tool": tool_name,
                    "tool_args": tool_args,
                    "message": f"Codex CLI completed {tool_name}",
                    "tool_result": tool_result,
                }
                if operation_id:
                    payload["operation_id"] = operation_id
                    tool_result["tool_call_id"] = operation_id
                event_callback(
                    "tool_end",
                    payload,
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
        for key in ("name", "tool", "title", "type"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                if self._looks_like_shell_command(value):
                    return "shell_command"
                return value.strip()
        return "item"

    def _stream_tool_context(self, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        item_type = str(item.get("type") or "").lower()
        if item_type in {"command", "command_execution", "shell"}:
            command = self._stream_item_command(item)
            args: dict[str, Any] = {"item_type": item_type or "command"}
            if command:
                args["command"] = command
            return "shell_command", args

        if item_type in {"file_change", "file_edit"}:
            changes = item.get("changes")
            args = {"changes": changes} if isinstance(changes, list) else {}
            paths = [
                str(change.get("path"))
                for change in changes or []
                if isinstance(change, dict) and change.get("path")
            ]
            if len(paths) == 1:
                args["path"] = paths[0]
            elif paths:
                args["paths"] = paths
            return "write_file", args

        if item_type == "web_search":
            query = item.get("query") or item.get("text")
            return "web_search", {"query": query} if query else {}

        label = self._stream_item_label(item)
        args = {}
        raw_args = item.get("arguments") or item.get("args")
        if isinstance(raw_args, dict):
            args = raw_args
        return label, args

    def _stream_item_command(self, item: dict[str, Any]) -> str:
        for key in ("command", "cmd", "title", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _looks_like_shell_command(self, value: str) -> bool:
        lower = value.strip().lower()
        return any(
            marker in lower
            for marker in (
                "powershell.exe",
                "\\pwsh.exe",
                "/pwsh",
                "cmd.exe",
                " -command ",
                " -command'",
                " -command\"",
                " /c ",
                " -c ",
            )
        )

    def _stream_item_output(self, item: dict[str, Any]) -> str:
        for key in ("aggregated_output", "output", "text", "result"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(item.get("changes"), list):
            return json.dumps(item["changes"], ensure_ascii=False, default=str)
        return ""

    def _is_tool_like_item(self, item: dict[str, Any]) -> bool:
        item_type = str(item.get("type") or "").lower()
        return item_type in {
            "command",
            "command_execution",
            "function_call",
            "tool_call",
            "shell",
            "file_change",
            "file_edit",
            "mcp_tool_call",
            "web_search",
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
        timeout: Optional[int] = None,
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
