"""Grok Build CLI backend implementation.

Grok Build is intentionally used through its local CLI rather than the xAI
API.  Headless output is newline-delimited JSON, so this backend normalizes
the provider events into AoiTalk's existing status/tool timeline events while
leaving AoiTalk's own ``[TOOL_CALL: ...]`` follow-up loop unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Optional

from .base import CLIBackendBase, CLIEventCallback

_AUTH_MARKERS = (
    "not logged in",
    "not authenticated",
    "unauthenticated",
    "authentication required",
    "run `grok login`",
    "grok login",
    "cached_token",
)
_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "quota",
    "too many requests",
    "weekly limit",
    "limit reached",
)
_FINAL_TYPES = {
    "final",
    "final_message",
    "result",
    "response",
    "assistant_message",
    "assistant_response",
    "assistant",
    "completion",
    "completed",
    "message",
    "text",
    "content",
}


def _text_value(value: Any) -> str:
    """Extract text from the scalar/list shapes used by JSON stream events."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "value", "output", "message", "content", "result"):
            text = _text_value(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        return "".join(_text_value(item) for item in value)
    return ""


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "event", "update", "params"):
        value = event.get(key)
        if isinstance(value, dict):
            nested = value.get("update") if key == "params" else value
            if isinstance(nested, dict):
                return nested
    return event


class GrokCLIBackend(CLIBackendBase):
    """Grok Build CLI backend for AoiTalk's normal CLI client."""

    def __init__(self, model: Optional[str] = None):
        self._model = model
        super().__init__()

    def get_cli_command(self, prompt: str) -> List[str]:
        bin_path = os.getenv("GROK_BIN", "grok")
        cmd = [
            bin_path,
            "--no-auto-update",
            "--output-format",
            "streaming-json",
        ]
        model = self._model or os.getenv("GROK_MODEL")
        if model:
            cmd.extend(["--model", model])
        if prompt:
            cmd.extend(["-p", prompt])
        return cmd

    def get_provider_name(self) -> str:
        return "Grok Build CLI"

    def get_cwd_args(self, cwd: Optional[Path]) -> List[str]:
        if cwd is None:
            return []
        return ["--cwd", str(cwd.resolve())]

    def parse_output(self, raw_output: str) -> str:
        final_messages: list[str] = []
        deltas: list[str] = []
        for event in self._events(raw_output):
            payload = _event_payload(event)
            event_type = self._event_type(event, payload)
            text = self._event_text(event, payload)
            if not text:
                continue
            if self._is_delta(event, payload, event_type):
                deltas.append(text)
            elif self._is_final(event_type, payload):
                final_messages.append(text.strip())

        if final_messages:
            return final_messages[-1]
        if deltas:
            return "".join(deltas).strip()
        return super().parse_output(raw_output)

    def handle_stream_output_line(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        event = self._parse_event(line)
        if event is None:
            return

        payload = _event_payload(event)
        event_type = self._event_type(event, payload)
        normalized = event_type.lower().replace("-", "_")
        operation_id = str(
            payload.get("id")
            or payload.get("call_id")
            or payload.get("tool_call_id")
            or event.get("id")
            or event.get("call_id")
            or ""
        ).strip()
        if normalized in {"error", "failed", "failure"}:
            message = self._event_text(event, payload) or "Grok Build CLI error"
            event_callback(
                "status_update",
                {"status": "grok_cli_error", "message": message},
            )
            return

        if normalized in {"tool", "tool_start", "tool_started", "tool_use", "tool_call", "command", "command_execution", "shell"}:
            tool_name, args = self._tool_context(payload)
            tool_event = {
                "tool": tool_name,
                "tool_args": args,
                "message": f"Grok Build CLI started {tool_name}",
            }
            if operation_id:
                tool_event["operation_id"] = operation_id
            event_callback(
                "tool_start",
                tool_event,
            )
            return

        if normalized in {"tool_end", "tool_completed", "tool_result", "command_completed", "command_execution_completed"}:
            tool_name, args = self._tool_context(payload)
            tool_result = {
                "tool": tool_name,
                "arguments": args,
                "output": self._event_text(event, payload),
            }
            error = payload.get("error")
            if error:
                tool_result["error"] = error
            if payload.get("stderr"):
                tool_result["stderr"] = payload["stderr"]
            exit_code = payload.get("exit_code")
            if exit_code is not None:
                tool_result["exit_code"] = exit_code
            tool_event = {
                "tool": tool_name,
                "tool_args": args,
                "message": f"Grok Build CLI completed {tool_name}",
                "tool_result": tool_result,
            }
            if operation_id:
                tool_event["operation_id"] = operation_id
                tool_result["tool_call_id"] = operation_id
            event_callback(
                "tool_end",
                tool_event,
            )
            return

        if normalized in {"turn_started", "session_started", "run_started", "message_start"}:
            event_callback(
                "status_update",
                {"status": f"grok_{normalized}", "message": f"Grok Build CLI {normalized}"},
            )
            return

        if normalized in {"turn_completed", "session_completed", "run_completed", "message_stop", "done"}:
            event_callback(
                "status_update",
                {"status": f"grok_{normalized}", "message": f"Grok Build CLI {normalized}"},
            )
            return

        text = self._event_text(event, payload)
        if text and self._is_delta(event, payload, normalized):
            event_callback(
                "status_update",
                {"status": "grok_text_delta", "message": text},
            )

    def parse_error_output(self, stdout: str, stderr: str, exit_code: int) -> Optional[str]:
        messages: list[str] = []
        for event in self._events(stdout):
            payload = _event_payload(event)
            event_type = self._event_type(event, payload).lower()
            if event_type in {"error", "failed", "failure", "turn_failed"}:
                text = self._event_text(event, payload)
                if text:
                    messages.append(text.strip())
        message = messages[-1] if messages else f"{stdout}\n{stderr}".strip()
        lower = message.lower()
        if any(marker in lower for marker in _AUTH_MARKERS):
            return "Grok Build CLI が未認証です。`grok login` を実行するか、XAI_API_KEYを設定してください。"
        if any(marker in lower for marker in _LIMIT_MARKERS):
            return "Grok Build CLI の利用上限またはレート制限に達しました。時間を置くか別のモデルへ切り替えてください。"
        if message:
            return f"Grok Build CLI の実行に失敗しました: {message[:2000]}"
        return None

    @classmethod
    def _parse_event(cls, line: str) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @classmethod
    def _events(cls, raw_output: str):
        for line in raw_output.splitlines():
            event = cls._parse_event(line)
            if event is not None:
                yield event

    @staticmethod
    def _event_type(event: dict[str, Any], payload: dict[str, Any]) -> str:
        value = event.get("type") or payload.get("type")
        if not value:
            value = payload.get("sessionUpdate") or payload.get("event")
        if not value:
            value = event.get("method")
        return (
            str(value or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(".", "_")
        )

    @staticmethod
    def _event_text(event: dict[str, Any], payload: dict[str, Any]) -> str:
        for source in (event, payload):
            for key in ("text", "delta", "content", "output", "message", "result", "error"):
                text = _text_value(source.get(key))
                if text:
                    return text
            for key in ("tool", "item", "data"):
                text = _text_value(source.get(key))
                if text:
                    return text
        return ""

    @staticmethod
    def _is_delta(event: dict[str, Any], payload: dict[str, Any], event_type: str) -> bool:
        if any(key in event or key in payload for key in ("delta", "chunk")):
            return True
        return event_type.endswith("_chunk") or event_type in {
            "content_block_delta",
            "agent_message_chunk",
            "text_delta",
        }

    @staticmethod
    def _is_final(event_type: str, payload: dict[str, Any]) -> bool:
        if event_type in _FINAL_TYPES:
            return True
        if event_type == "item_completed" and str(payload.get("role") or "").lower() == "assistant":
            return True
        return event_type.endswith("_final")

    @staticmethod
    def _tool_context(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        item = payload.get("tool") or payload.get("item") or payload
        if not isinstance(item, dict):
            item = payload
        raw_name = item.get("name") or item.get("tool_name") or item.get("tool") or item.get("type")
        tool_name = str(raw_name or "grok_tool").strip()
        command = item.get("command") or item.get("cmd")
        if tool_name in {"command", "command_execution", "shell"}:
            tool_name = "shell_command"
        args = item.get("arguments") or item.get("args") or item.get("input") or {}
        if not isinstance(args, dict):
            args = {"value": args}
        if command and "command" not in args:
            args = {**args, "command": command}
        return tool_name, args
