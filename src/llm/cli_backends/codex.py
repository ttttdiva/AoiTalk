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
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..generation_policy import (
    GenerationProfile,
    PermissionPolicy,
    get_current_generation_policy,
)
from ..tool_policy import get_current_user_input, looks_like_managed_workspace_request
from .base import CLIBackendBase, CLIEventCallback, CLISessionCapabilities
from ..turn_stream_events import emit_assistant_text, emit_thinking

logger = logging.getLogger(__name__)

# Codex JSONL で思考（要約）として扱う item type。
_REASONING_ITEM_TYPES = {"reasoning", "agent_reasoning", "thinking"}

_USAGE_LIMIT_PREFIX = "You've hit your usage limit for "

# ``None`` is a meaningful override for Codex: it explicitly disables both
# the configured model and CODEX_MODEL so the CLI can select the account's
# default model.  Keep a sentinel to distinguish that from the normal path.
_MODEL_OVERRIDE_UNSET = object()
_active_model_override: ContextVar[object] = ContextVar(
    "codex_active_model_override",
    default=_MODEL_OVERRIDE_UNSET,
)
_active_disable_native_tools: ContextVar[bool] = ContextVar(
    "codex_disable_native_tools",
    default=False,
)
_active_agent_team_cli: ContextVar[dict[str, Any] | None] = ContextVar(
    "codex_active_agent_team_cli",
    default=None,
)


@contextmanager
def agent_team_cli_context(*, workspace_access: str = "read"):
    """Mark a Codex invocation as Agent Team CLI-native execution.

    This opt-in keeps ordinary chat/managed-workspace mediation unchanged;
    only v2 templates with an explicit CLI-native allowance can set it.
    """
    access = str(workspace_access or "read").strip().lower()
    if access not in {"read", "write"}:
        access = "read"
    token = _active_agent_team_cli.set({"workspace_access": access})
    try:
        yield
    finally:
        _active_agent_team_cli.reset(token)

_CODEX_ADAPTER_PREAMBLE = """\
You are the model backend inside the AoiTalk app.
Use the AoiTalk tools listed in the system context through the documented
[TOOL_CALL: name(args...)] format whenever the task requires file access, web
search, project DB updates, or other external actions that AoiTalk exposes.
Treat tool results as the only proof that external work happened, and continue
requesting the next needed tool call when a result reveals required follow-up
work. Return the final user-facing assistant message when the available tool
results are enough to answer accurately.
For AoiTalk Project workspace or Docs operations, never use native shell or
native file tools, never inspect the AoiTalk source repository, and never write
the database directly. Use only the listed high-level AoiTalk tools.
"""

_AGENT_TEAM_CLI_PREAMBLE = """\
You are an Agent Team CLI worker inside AoiTalk.
Use provider-native filesystem/search/shell/edit/test/build tools for the
repository workspace within the assigned sandbox. Use the listed AoiTalk
high-level tools for Docs, Tasks, Projects, Calendar, WBS, Memory, and
permissions. For ad-hoc Python analysis, use `python`; it resolves to the
AoiTalk runtime environment. If the target workspace has its own Python
environment, prefer that project-specific interpreter when appropriate. Never
connect to or mutate PostgreSQL directly. Keep all writes inside the assigned
workspace boundary and report concise evidence.
"""


class CodexCLIBackend(CLIBackendBase):
    """Codex CLI backend implementation."""

    scoped_execution_delegate = True

    _native_sessions_available: Optional[bool] = None

    def __init__(
        self,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ):
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._image_paths: list[Path] = []
        self._stream_round = 0
        self._usage_event_index = 0
        # 最終出力は最後の agent_message なので、確定するまで1件保留する。
        self._pending_agent_message: Optional[tuple[str, int]] = None
        super().__init__()

    def _reset_stream_state(self) -> None:
        self._stream_round = 0
        self._usage_event_index = 0
        self._pending_agent_message = None

    def get_session_capabilities(self) -> CLISessionCapabilities:
        if self.__class__._native_sessions_available is None:
            self.__class__._native_sessions_available = self.cli_help_contains(
                os.getenv("CODEX_BIN", "codex"),
                ["exec", "--help"],
                "resume",
                "--json",
            )
        supported = bool(self.__class__._native_sessions_available)
        return CLISessionCapabilities(
            native_sessions=supported,
            supports_resume=supported,
            supports_follow_up=supported,
            fallback_to_stateless=True,
            supports_explicit_session_id=False,
            supports_detach=False,
        )

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        for line in str(raw_output or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if str(event.get("type") or "") == "thread.started":
                value = event.get("thread_id") or event.get("threadId")
                if value:
                    return str(value).strip()
        return super().extract_native_session_id(raw_output)

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build a Codex CLI command."""
        policy = get_current_generation_policy()
        managed_workspace_request = looks_like_managed_workspace_request(
            get_current_user_input() or ""
        )
        plain_generation = _active_disable_native_tools.get()
        agent_team_cli = _active_agent_team_cli.get()
        if agent_team_cli:
            # Agent Team CLI-native path is distinct from the normal managed
            # workspace path.  The provider still gets a sandbox, but native
            # shell/search/edit tools are intentionally retained.
            managed_workspace_request = False
            plain_generation = False
        bin_path = os.getenv("CODEX_BIN", "codex")
        action = str(getattr(self, "_active_native_session_action", "stateless"))
        session_id = str(getattr(self, "_active_native_session_id", "") or "").strip()
        cmd = [
            bin_path,
            "exec",
            "--json",
            "--color",
            "never",
        ]
        ephemeral = getattr(self, "_active_native_session_ephemeral", None)
        if ephemeral is None:
            # Existing specialist/internal calls remain isolated by default.
            ephemeral = True
        if ephemeral:
            cmd.append("--ephemeral")
        if policy.profile == GenerationProfile.CHAT or managed_workspace_request or plain_generation:
            cmd.append("--ignore-rules")
        if managed_workspace_request or plain_generation:
            # Managed workspace operations must be mediated by AoiTalk's
            # high-level tools.  A read-only sandbox still exposes Codex's
            # native shell for reads, so isolate this invocation from user
            # configuration and remove both shell implementations entirely.
            cmd.extend(
                [
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--disable",
                    "shell_tool",
                    "--disable",
                    "unified_exec",
                ]
            )

        model_override = _active_model_override.get()
        model = (
            str(model_override or "").strip()
            if model_override is not _MODEL_OVERRIDE_UNSET
            else self._model or os.getenv("CODEX_MODEL")
        )
        if model:
            cmd.extend(["--model", model])

        for image_path in self._image_paths:
            cmd.extend(["--image", str(image_path)])

        reasoning_effort = (
            self._reasoning_effort
            if self._reasoning_effort is not None
            else os.getenv("CODEX_REASONING_EFFORT")
        )
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        approval_policy = (
            "never"
            if policy.permission_policy == PermissionPolicy.AUTO_APPROVE
            else os.getenv("CODEX_APPROVAL_POLICY")
        )
        if approval_policy:
            cmd.extend(["-c", f'approval_policy="{approval_policy.strip()}"'])

        sandbox = (
            str(agent_team_cli.get("workspace_access") or "read-only")
            if agent_team_cli
            else "read-only" if managed_workspace_request or plain_generation else os.getenv("CODEX_SANDBOX")
        )
        if agent_team_cli:
            sandbox = "workspace-write" if sandbox == "write" else "read-only"
        if sandbox is None:
            sandbox = (
                "workspace-write"
                if (
                    policy.permission_policy == PermissionPolicy.AUTO_APPROVE
                    and not managed_workspace_request
                )
                else "read-only"
            )
        sandbox = sandbox.strip()
        if sandbox:
            cmd.extend(["--sandbox", sandbox])

        if action == "resume" and session_id:
            cmd.extend(["resume", session_id])
            # ``-`` is Codex's documented marker for reading the resumed
            # turn from stdin. The AoiTalk adapter uses stdin so system/delta
            # context cannot be truncated into a command-line argument.
            if not prompt:
                cmd.append("-")
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
        usage_event_index = 0
        for line in raw_output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed":
                usage_key = (
                    f"codex:{self._usage_invocation_id}:turn:{usage_event_index}"
                )
                self.set_last_usage(
                    event.get("usage"),
                    usage_key=usage_key,
                )
                usage_event_index += 1
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
            # 保留中の agent_message は最終出力そのものなので配信しない。
            self._pending_agent_message = None
            usage_index = self._usage_event_index
            self._usage_event_index += 1
            usage_key = f"codex:{self._usage_invocation_id}:turn:{usage_index}"
            normalized_usage = self.set_last_usage(
                event.get("usage"),
                usage_key=usage_key,
            )
            payload: dict[str, Any] = {
                "status": "codex_turn_completed",
                "message": "Codex CLI turn completed",
                "usage_key": usage_key,
            }
            if normalized_usage is not None:
                payload["usage"] = normalized_usage
            event_callback("status_update", payload)
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
            elif item_type == "agent_message":
                self._handle_agent_message_item(item, event_callback)
            elif item_type in _REASONING_ITEM_TYPES:
                # Codex の reasoning は要約された思考なので kind=summary で配信する。
                emit_thinking(
                    event_callback,
                    self._stream_item_output(item),
                    round_index=self._stream_round,
                    kind="summary",
                )
            elif item_type:
                event_callback(
                    "status_update",
                    {
                        "status": f"codex_{item_type}_completed",
                        "message": f"Codex CLI completed {label}",
                    },
                )

    def _handle_agent_message_item(
        self,
        item: dict[str, Any],
        event_callback: CLIEventCallback,
    ) -> None:
        """途中の agent_message だけを assistant_text として配信する。

        最後の agent_message は ``parse_output`` が最終出力として返すため、
        次の agent_message か turn.completed が来るまで保留する。
        """
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        pending = self._pending_agent_message
        if pending is not None:
            emit_assistant_text(
                event_callback,
                pending[0],
                round_index=pending[1],
            )
        self._pending_agent_message = (text.strip(), self._stream_round)
        self._stream_round += 1

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
        *,
        native_session_id: Optional[str] = None,
        native_session_action: str = "stateless",
        ephemeral: Optional[bool] = None,
        model_override: object = _MODEL_OVERRIDE_UNSET,
        disable_native_tools: bool = False,
    ):
        """Codex accepts one initial instruction stream, so pass it via stdin."""
        self._reset_stream_state()
        model_override_token = None
        disable_native_tools_token = None
        if model_override is not _MODEL_OVERRIDE_UNSET:
            model_override_token = _active_model_override.set(model_override)
        if disable_native_tools:
            disable_native_tools_token = _active_disable_native_tools.set(True)
        policy = get_current_generation_policy()
        combined_prompt = prompt
        if system_context:
            combined_prompt = (
                f"{system_context}\n\nUser request:\n{prompt}" if prompt else system_context
            )
        if str(native_session_action or "stateless") != "resume":
            preamble = _AGENT_TEAM_CLI_PREAMBLE if _active_agent_team_cli.get() else _CODEX_ADAPTER_PREAMBLE
            combined_prompt = f"{preamble}\n\n{combined_prompt}"

        try:
            return super().execute_prompt(
                "",
                cwd=cwd,
                timeout=timeout,
                extra_args=extra_args,
                system_context=combined_prompt,
                event_callback=event_callback,
                native_session_id=native_session_id,
                native_session_action=native_session_action,
                ephemeral=ephemeral,
            )
        finally:
            if disable_native_tools_token is not None:
                _active_disable_native_tools.reset(disable_native_tools_token)
            if model_override_token is not None:
                _active_model_override.reset(model_override_token)

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Codex CLI does not support runtime MCP arguments."""
        if mcp_servers:
            logger.info(
                "[Codex CLI] %s MCP server(s) in config.yaml. "
                "Codex CLI requires MCP to be configured in ~/.codex/config.toml",
                len(mcp_servers),
            )
        return []
