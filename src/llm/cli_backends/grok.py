"""Grok Build CLI backend implementation.

Grok Build is intentionally used through its local CLI rather than the xAI
API.  Headless output is newline-delimited JSON, so this backend normalizes
the provider events into AoiTalk's existing status/tool timeline events while
leaving AoiTalk's own ``[TOOL_CALL: ...]`` follow-up loop unchanged.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, List, Optional

from .base import (
    CLIBackendBase,
    CLIEventCallback,
    CLIInvocationOptions,
    CLISessionCapabilities,
)
from ..turn_stream_events import emit_assistant_text

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
_HTTP_ERROR_MARKERS = (
    "http 5",
    "http 4",
    "internal server error",
    "bad gateway",
    "service unavailable",
)
_FINAL_TYPES = {
    "final",
    "final_message",
    "result",
    "response",
    "assistant_message",
    "assistant_response",
    "agent_message_end",
    "assistant_message_end",
    "agent_message_completed",
    "assistant_message_completed",
    "assistant",
    "completion",
}
_COMPLETION_TYPES = {
    "turn_completed",
    "session_completed",
    "run_completed",
    "message_stop",
    "done",
    "end",
}
_CANCEL_TYPES = {"cancelled", "canceled", "turn_cancelled", "turn_canceled"}


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

    scoped_execution_delegate = True
    provider_state_env_name = "GROK_HOME"
    provider_state_root_supported = True

    _native_sessions_available: Optional[bool] = None

    def __init__(self, model: Optional[str] = None):
        self._model = model
        # text delta を溜め、ツール呼び出し境界でまとまった assistant_text にする。
        self._text_delta_buffer: list[str] = []
        self._stream_round = 0
        self._stream_terminal_seen = False
        super().__init__()

    def _reset_stream_state(self) -> None:
        self._text_delta_buffer = []
        self._stream_round = 0
        self._stream_terminal_seen = False

    def get_session_capabilities(self) -> CLISessionCapabilities:
        if self.__class__._native_sessions_available is None:
            self.__class__._native_sessions_available = self.cli_help_contains(
                os.getenv("GROK_BIN", "grok"),
                ["--help"],
                "--resume",
                "--session-id",
                "--output-format",
            )
        supported = bool(self.__class__._native_sessions_available)
        return CLISessionCapabilities(
            native_sessions=supported,
            supports_resume=supported,
            supports_follow_up=supported,
            fallback_to_stateless=True,
            supports_explicit_session_id=supported,
            supports_detach=False,
        )

    def create_native_session_id(self) -> Optional[str]:
        return str(uuid.uuid4())

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        for event in self._events(raw_output):
            payload = _event_payload(event)
            event_type = self._event_type(event, payload)
            if event_type in {
                "session_started",
                "session_completed",
                "run_completed",
                "end",
                "done",
            } or any(key in event or key in payload for key in ("sessionId", "session_id")):
                for source in (event, payload):
                    for key in ("sessionId", "session_id", "conversationId", "conversation_id"):
                        value = source.get(key)
                        if value:
                            return str(value).strip()
        return super().extract_native_session_id(raw_output)

    def validate_invocation_options(self, options: CLIInvocationOptions) -> None:
        super().validate_invocation_options(options)
        if options.effective_effort:
            raise ValueError("Grok Build CLI does not support per-invocation effort")
        if options.sandbox is not None:
            raise ValueError("Grok Build CLI does not expose a per-invocation sandbox flag")
        if options.effective_approval_policy not in {
            "native_default",
            "standard",
        }:
            raise ValueError(
                f"Grok Build CLI does not support approval policy: {options.effective_approval_policy}"
            )

    def execute_prompt(self, *args: Any, **kwargs: Any):
        self._reset_stream_state()
        return super().execute_prompt(*args, **kwargs)

    def _flush_text_delta_buffer(self, event_callback: CLIEventCallback) -> None:
        """溜めた delta をまとまった途中経過テキストとして配信する。"""
        buffered = "".join(self._text_delta_buffer).strip()
        self._text_delta_buffer = []
        if not buffered:
            return
        emit_assistant_text(
            event_callback,
            buffered,
            round_index=self._stream_round,
        )
        self._stream_round += 1

    @staticmethod
    def _extract_applied_launch_fields(
        cmd: List[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        approval, permission, sandbox = CLIBackendBase._extract_applied_launch_fields(cmd)
        if "--always-approve" in cmd:
            return "never", "auto", sandbox
        return approval, permission, sandbox

    def get_cli_command(self, prompt: str) -> List[str]:
        bin_path = os.getenv("GROK_BIN", "grok")
        cmd = [
            bin_path,
            "--always-approve",
            "--no-auto-update",
            "--output-format",
            "streaming-json",
        ]
        action = str(getattr(self, "_active_native_session_action", "stateless"))
        session_id = str(getattr(self, "_active_native_session_id", "") or "").strip()
        if action == "resume" and session_id:
            cmd.extend(["--resume", session_id])
        elif action == "start" and session_id:
            cmd.extend(["--session-id", session_id])
        options = self.get_invocation_options()
        model = (
            str(options.model or "").strip()
            if options is not None
            else self._model or os.getenv("GROK_MODEL")
        )
        if model:
            cmd.extend(["--model", model])
        # --always-approve is backend-owned; never pass a conflicting
        # permission mode from an invocation or resumed session.
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
        events = list(self._events(raw_output))
        terminal_messages: list[tuple[int, str]] = []
        completions: list[int] = []
        deltas: list[tuple[int, str]] = []
        for index, event in enumerate(events):
            payload = _event_payload(event)
            event_type = self._event_type(event, payload)
            text = self._event_text(event, payload)
            if self._is_terminal_assistant_event(event, payload, event_type):
                terminal_messages.append((index, text.strip()))
                continue
            if self._is_completion_event(event, payload, event_type):
                completions.append(index)
            if (
                text
                and self._is_assistant_event(event, payload, event_type)
                and self._is_delta(event, payload, event_type)
            ):
                deltas.append((index, text))

        if terminal_messages:
            terminal_index, terminal_text = terminal_messages[-1]
            if terminal_text:
                return terminal_text
            # Some CLIs terminate the assistant item separately from its final
            # text chunks. Use only the chunks belonging to that item.
            previous_terminal = terminal_messages[-2][0] if len(terminal_messages) > 1 else -1
            return "".join(
                text for index, text in deltas if previous_terminal < index < terminal_index
            ).strip()
        if completions:
            completion_index = completions[-1]
            previous_completion = completions[-2] if len(completions) > 1 else -1
            return "".join(
                text
                for index, text in deltas
                if previous_completion < index < completion_index
            ).strip()
        if deltas:
            return "".join(text for _, text in deltas).strip()
        if events:
            # Structured tool/status events must never leak their JSON payload
            # as the assistant response.
            return ""
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
        failure_kind, _ = self._event_failure(event, payload, normalized)
        if failure_kind == "stream_cancelled":
            self._text_delta_buffer = []
            event_callback(
                "status_update",
                {
                    "status": "grok_cli_cancelled",
                    "message": "Grok Build CLI turn was cancelled",
                },
            )
            return
        operation_id = str(
            payload.get("id")
            or payload.get("call_id")
            or payload.get("tool_call_id")
            or event.get("id")
            or event.get("call_id")
            or ""
        ).strip()
        if self._is_terminal_assistant_event(event, payload, normalized):
            self._text_delta_buffer = []
            self._stream_terminal_seen = True
            return

        if normalized in {"error", "failed", "failure"}:
            message = self._event_text(event, payload) or "Grok Build CLI error"
            event_callback(
                "status_update",
                {"status": "grok_cli_error", "message": message},
            )
            return

        if normalized in {"tool", "tool_start", "tool_started", "tool_use", "tool_call", "command", "command_execution", "shell"}:
            # ツール呼び出し直前までのテキストは途中経過として確定する。
            self._flush_text_delta_buffer(event_callback)
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
            self._flush_text_delta_buffer(event_callback)
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
            self._text_delta_buffer = []
            self._stream_terminal_seen = False
            event_callback(
                "status_update",
                {"status": f"grok_{normalized}", "message": f"Grok Build CLI {normalized}"},
            )
            return

        if normalized in {
            "turn_completed",
            "session_completed",
            "run_completed",
            "message_stop",
            "done",
            "end",
        }:
            # 完了時点で残っている delta は最終回答そのもの（parse_output が返す）ため、
            # assistant_text として重複配信せず破棄する。
            self._text_delta_buffer = []
            self._stream_terminal_seen = True
            event_callback(
                "status_update",
                {"status": f"grok_{normalized}", "message": f"Grok Build CLI {normalized}"},
            )
            return

        text = self._event_text(event, payload)
        if text and self._is_delta(event, payload, normalized):
            if self._stream_terminal_seen:
                return
            self._text_delta_buffer.append(text)
            # 既存の逐次表示は互換のためそのまま残す。
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
        if any(marker in lower for marker in _HTTP_ERROR_MARKERS):
            return f"Grok Build CLI returned an HTTP error: {message[:2000]}"
        if message:
            return f"Grok Build CLI の実行に失敗しました: {message[:2000]}"
        return None

    def validate_success_output(self, raw_output: str, stderr: str) -> Optional[str]:
        """Reject successful process exits without a complete assistant reply."""

        raw_output = str(raw_output or "")
        events = list(self._events(raw_output))
        for event in events:
            payload = _event_payload(event)
            event_type = self._event_type(event, payload).lower()
            failure_kind, detail = self._event_failure(event, payload, event_type)
            if failure_kind:
                self._set_cli_diagnostic(
                    status="failed",
                    state="failed",
                    error_code=f"grok_{failure_kind}",
                )
                if failure_kind == "stream_cancelled":
                    return "Grok Build CLI turn was cancelled."
                classified = self.parse_error_output(raw_output, stderr, 0)
                if classified and (
                    "未認証" in classified
                    or "利用上限" in classified
                    or "HTTP error" in classified
                ):
                    return classified
                return f"Grok Build CLI reported a failed result: {detail[:1000]}"

        if not raw_output.strip():
            self._set_cli_diagnostic(
                status="failed", state="failed", error_code="grok_empty_response"
            )
            return "Grok Build CLI returned no assistant response."

        if not events:
            lower_output = raw_output.lower()
            if any(
                marker in lower_output
                for marker in (*_AUTH_MARKERS, *_LIMIT_MARKERS, *_HTTP_ERROR_MARKERS)
            ):
                self._set_cli_diagnostic(
                    status="failed", state="failed", error_code="grok_provider_error"
                )
                return self.parse_error_output(raw_output, stderr, 0)

        malformed_lines = self._malformed_json_lines(raw_output)
        if malformed_lines:
            self._set_cli_diagnostic(
                status="failed", state="failed", error_code="grok_truncated_stream"
            )
            return "Grok Build CLI returned truncated or invalid streaming JSON."

        terminal_answer = self._terminal_assistant_output(events)
        if not terminal_answer:
            self._set_cli_diagnostic(
                status="failed", state="failed", error_code="grok_terminal_response_missing"
            )
            return "Grok Build CLI stream ended without a terminal assistant response."
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

    @classmethod
    def _malformed_json_lines(cls, raw_output: str) -> list[str]:
        return [
            line
            for line in raw_output.splitlines()
            if line.strip() and cls._parse_event(line) is None
        ]

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
            for key in (
                "text",
                "delta",
                "content",
                "output",
                "message",
                "response",
                "result",
                "error",
            ):
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
            "text",
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

    @classmethod
    def _is_terminal_assistant_event(
        cls,
        event: dict[str, Any],
        payload: dict[str, Any],
        event_type: str,
    ) -> bool:
        if not cls._is_assistant_event(event, payload, event_type):
            return False
        if cls._is_final(event_type, payload):
            return True
        item = payload.get("item")
        if event_type == "item_completed" and isinstance(item, dict):
            if str(item.get("role") or "").lower() == "assistant":
                return True
        return any(
            bool(source.get(key))
            for source in (event, payload)
            for key in ("is_final", "final", "terminal")
        )

    @classmethod
    def _is_assistant_event(
        cls,
        event: dict[str, Any],
        payload: dict[str, Any],
        event_type: str,
    ) -> bool:
        normalized = event_type.lower().replace("-", "_")
        if (
            normalized.startswith("tool_")
            or normalized in {
                "tool",
                "tool_start",
                "tool_started",
                "tool_use",
                "tool_call",
                "tool_end",
                "tool_completed",
                "tool_result",
                "command",
                "command_execution",
                "command_execution_completed",
                "shell",
            }
        ):
            return False
        sources = [event, payload]
        for source in (event, payload):
            for key in ("item", "content_block", "message", "result"):
                nested = source.get(key)
                if isinstance(nested, dict):
                    sources.append(nested)
        for source in sources:
            nested_type = str(source.get("type") or source.get("kind") or "").lower()
            if nested_type.startswith("tool_") or nested_type in {
                "tool",
                "tool_call",
                "tool_result",
                "command_execution",
                "shell",
            }:
                return False
            role = source.get("role") or source.get("speaker") or source.get("author")
            if role and str(role).strip().lower() not in {"assistant", "agent"}:
                return False
        if payload.get("tool"):
            return False
        return True

    @classmethod
    def _is_completion_event(
        cls,
        event: dict[str, Any],
        payload: dict[str, Any],
        event_type: str,
    ) -> bool:
        return event_type in _COMPLETION_TYPES and cls._is_assistant_event(
            event, payload, event_type
        )

    @classmethod
    def _terminal_assistant_output(cls, events: list[dict[str, Any]]) -> str:
        terminal_messages: list[tuple[int, str]] = []
        completions: list[int] = []
        deltas: list[tuple[int, str]] = []
        for index, event in enumerate(events):
            payload = _event_payload(event)
            event_type = cls._event_type(event, payload)
            text = cls._event_text(event, payload)
            if cls._is_terminal_assistant_event(event, payload, event_type):
                terminal_messages.append((index, text.strip()))
                continue
            if cls._is_completion_event(event, payload, event_type):
                completions.append(index)
            if (
                text
                and cls._is_assistant_event(event, payload, event_type)
                and cls._is_delta(event, payload, event_type)
            ):
                deltas.append((index, text))

        if terminal_messages:
            terminal_index, terminal_text = terminal_messages[-1]
            if terminal_text:
                return terminal_text
            previous_terminal = terminal_messages[-2][0] if len(terminal_messages) > 1 else -1
            return "".join(
                text for index, text in deltas if previous_terminal < index < terminal_index
            ).strip()
        if completions:
            completion_index = completions[-1]
            previous_completion = completions[-2] if len(completions) > 1 else -1
            return "".join(
                text
                for index, text in deltas
                if previous_completion < index < completion_index
            ).strip()
        return ""

    @classmethod
    def _event_failure(
        cls,
        event: dict[str, Any],
        payload: dict[str, Any],
        event_type: str,
    ) -> tuple[Optional[str], str]:
        status_values: list[Any] = []
        for source in (event, payload):
            status_values.extend(
                source.get(key)
                for key in ("status", "status_code", "http_status", "httpStatus")
                if source.get(key) is not None
            )
        result_payload = payload.get("result")
        if isinstance(result_payload, dict):
            status_values.extend(
                result_payload.get(key)
                for key in ("status", "status_code", "http_status", "httpStatus")
                if result_payload.get(key) is not None
            )
        statuses = {str(value).strip().lower() for value in status_values}
        detail = cls._event_text(event, payload).strip()
        error_sources = [event, payload]
        if isinstance(result_payload, dict):
            error_sources.append(result_payload)
        error_detail = " ".join(
            str(source.get(key) or "")
            for source in error_sources
            for key in ("error", "message")
        ).lower()
        normalized = event_type.lower().replace("-", "_")
        if normalized in _CANCEL_TYPES or statuses.intersection({"cancelled", "canceled"}):
            return "stream_cancelled", detail or "Provider cancelled the turn"
        if any(value.isdigit() and int(value) >= 400 for value in statuses):
            return "http_error", detail or f"HTTP status {', '.join(sorted(statuses))}"
        is_failure = normalized in {
            "error",
            "failed",
            "failure",
            "turn_failed",
            "result_failed",
        } or statuses.intersection({"error", "failed", "failure"})
        error_present = any(source.get("error") for source in error_sources)
        if is_failure or error_present:
            if any(marker in error_detail for marker in _AUTH_MARKERS):
                return "authentication_error", detail or "Grok Build CLI is not authenticated"
            if any(marker in error_detail for marker in _LIMIT_MARKERS):
                return "usage_limit", detail or "Grok Build CLI usage limit reached"
            if any(marker in error_detail for marker in _HTTP_ERROR_MARKERS):
                return "http_error", detail or "Provider returned an HTTP error"
            if is_failure:
                return "stream_error", detail or "Grok Build CLI reported a failed result"
        return None, ""

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
