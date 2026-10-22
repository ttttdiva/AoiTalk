"""
OpenAI Codex CLI backend implementation.

Usage: codex exec [--model MODEL] --json

MCP: Codex CLI reads MCP config from ~/.codex/config.toml
     Runtime capability gates use configuration overrides to disable servers.
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
from .base import (
    CLIBackendBase,
    CLIEventCallback,
    CLIInvocationOptions,
    CLISessionCapabilities,
)
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


def _resolve_sandbox(
    *,
    policy: Any,
    agent_team_cli: dict[str, Any] | None,
    managed_workspace_request: bool,
    plain_generation: bool,
) -> str:
    """Return the ``--sandbox`` value for one Codex invocation."""
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
    return sandbox.strip()


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
You are an Execution Team CLI worker inside AoiTalk.
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
    provider_state_env_name = "CODEX_HOME"
    provider_state_root_supported = True
    # Codex owns repository file search/read/edit and shell natively.
    native_repository_tools = True

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

    def native_repository_capabilities_for_scope(self, scope: Any) -> frozenset:
        """Native capabilities of the next invocation inside ``scope``.

        Managed-workspace and plain generations remove Codex's shell entirely,
        so nothing is native there.  Otherwise reads are native and edits/shell
        become native only when the resolved sandbox is ``workspace-write``.
        """
        agent_team_cli = _active_agent_team_cli.get()
        managed = looks_like_managed_workspace_request(get_current_user_input() or "")
        plain = _active_disable_native_tools.get()
        if agent_team_cli and not plain:
            managed = False
        if managed or plain:
            return frozenset()
        capabilities = {"read"}
        sandbox = _resolve_sandbox(
            policy=get_current_generation_policy(),
            agent_team_cli=agent_team_cli,
            managed_workspace_request=managed,
            plain_generation=plain,
        )
        if (
            sandbox == "workspace-write"
            and str(getattr(scope, "workspace_access_level", "") or "") == "write"
        ):
            capabilities.update({"edit", "shell"})
        return frozenset(capabilities)

    # Codex's CLI has no setting that removes the independent apply_patch
    # schema. shell_tool=false alone is not a complete filesystem/tool-free
    # gate; do not claim its plain-generation mode enforces that contract.
    # Upstream core/src/tools/spec_plan.rs registers ApplyPatchHandler from
    # model_info independently of ShellTool; the CLI does not expose ToolPolicy.
    supports_tool_free_capability_gates = False

    def native_feature_gate_args(self, disabled_features) -> list[str] | None:
        """Remove disabled native schemas and reject their direct invocation."""
        # MCP tables merge with host configuration: mcp_servers={} cannot
        # remove unenumerated host servers. There is no portable whole-MCP
        # exclusion flag, so refuse before starting the native CLI.
        if {"filesystem", "mcp"} & set(disabled_features):
            return None
        args: list[str] = []
        if "search" in disabled_features:
            args.extend(["-c", 'web_search="disabled"'])
        if disabled_features:
            # A native child must not reopen the disabled parent capability.
            args.extend(["--disable", "multi_agent", "--disable", "multi_agent_v2"])
        return args

    def native_mcp_gate_args(self, blocked_servers) -> list[str] | None:
        # enabled=false alone is invalid when the server is absent on the
        # host. Supplying a transport can conflict with a merged HTTP/stdio
        # entry. AoiTalk cannot prove the native host configuration matches
        # its registry; refuse instead of modifying an unknown transport.
        return None if blocked_servers else []

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

    def validate_invocation_options(self, options: CLIInvocationOptions) -> None:
        super().validate_invocation_options(options)
        if options.sandbox is not None:
            if isinstance(options.sandbox, bool):
                return
            value = str(options.sandbox).strip().lower()
            if value not in {
                "native_default",
                "restricted",
                "standard",
                "read-only",
                "workspace-write",
                "danger-full-access",
                "on-request",
                "never",
            }:
                raise ValueError(f"Codex CLI does not support sandbox mode: {options.sandbox}")
        policy = options.effective_approval_policy
        if policy in {"native_default", "standard"}:
            return
        if policy == "accept_edits":
            raise ValueError("Codex CLI does not support accept_edits permission mode")
        if policy not in {"on-request", "untrusted", "never"}:
            raise ValueError(f"Codex CLI does not support approval policy: {policy}")

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build a Codex CLI command."""
        policy = get_current_generation_policy()
        options = self.get_invocation_options()
        managed_workspace_request = looks_like_managed_workspace_request(
            get_current_user_input() or ""
        )
        plain_generation = _active_disable_native_tools.get()
        agent_team_cli = _active_agent_team_cli.get()
        if plain_generation:
            # Tool-free generation is a stricter contract than an ambient
            # Agent Team opt-in. It must never regain native tools.
            agent_team_cli = None
        if agent_team_cli:
            # Agent Team CLI-native path is distinct from the normal managed
            # workspace path.  The provider still gets a sandbox, but native
            # shell/search/edit tools are intentionally retained.
            managed_workspace_request = False
            plain_generation = False
        bin_path = os.getenv("CODEX_BIN", "codex")
        if self._active_run_scope() is not None:
            from ...security.wsl_bwrap_backend import scoped_codex_executable

            bin_path = scoped_codex_executable(bin_path)
        action = str(getattr(self, "_active_native_session_action", "stateless"))
        session_id = str(getattr(self, "_active_native_session_id", "") or "").strip()
        cmd = [
            bin_path,
            "exec",
            "--yolo",
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
        if options is None and (policy.profile == GenerationProfile.CHAT or managed_workspace_request or plain_generation):
            cmd.append("--ignore-rules")
        if options is None and (managed_workspace_request or plain_generation):
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

        if options is None and plain_generation:
            # Ignore user/project tool configuration and close optional host
            # capabilities as well as the two shell implementations above.
            for feature in (
                "apps", "plugins", "remote_plugin", "computer_use", "browser_use",
                "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
                "multi_agent", "multi_agent_v2", "image_generation", "view_image",
                "code_mode", "code_mode_host", "skill_search", "skill_mcp_dependency_install",
                "hooks", "workspace_dependencies", "tool_suggest", "auth_elicitation",
                "request_permissions_tool", "memories", "goals", "in_app_local_automation",
            ):
                cmd.extend(["--disable", feature])
            cmd.extend([
                "--enable", "skip_host_skill_discovery",
                "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0",
            ])

        if options is not None:
            model = str(options.model or "").strip()
        else:
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
            options.effective_effort
            if options is not None
            else (
                self._reasoning_effort
                if self._reasoning_effort is not None
                else os.getenv("CODEX_REASONING_EFFORT")
            )
        )
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

        if options is not None:
            requested_policy = options.effective_approval_policy
            approval_policy = {
                "standard": "never",
            }.get(requested_policy, requested_policy if requested_policy in {"never", "on-request", "untrusted"} else None)
            if requested_policy == "native_default":
                approval_policy = None
        else:
            approval_policy = (
                "never"
                if policy.permission_policy == PermissionPolicy.AUTO_APPROVE
                else os.getenv("CODEX_APPROVAL_POLICY")
            )
        if approval_policy:
            cmd.extend(["-c", f'approval_policy="{approval_policy.strip()}"'])

        if options is not None:
            if options.sandbox is None:
                sandbox = "workspace-write" if options.effective_approval_policy == "standard" else None
            elif isinstance(options.sandbox, bool):
                sandbox = "workspace-write" if options.sandbox else "read-only"
            else:
                requested_sandbox = str(options.sandbox).strip().lower()
                sandbox = {
                    "restricted": "read-only",
                    "standard": "workspace-write",
                    "native_default": None,
                }.get(requested_sandbox, requested_sandbox)
        else:
            sandbox = _resolve_sandbox(
                policy=policy,
                agent_team_cli=agent_team_cli,
                managed_workspace_request=managed_workspace_request,
                plain_generation=plain_generation,
            )
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

    def validate_success_output(self, raw_output: str, stderr: str) -> Optional[str]:
        """Reject Codex JSON failure events and missing final messages."""

        if self.get_invocation_options() is None:
            return None
        events: list[dict[str, Any]] = []
        for line in str(raw_output or "").splitlines():
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                events.append(value)
        if not events:
            self._set_cli_diagnostic(
                status="failed",
                state="failed",
                error_code="codex_missing_final_message",
            )
            return "Codex CLI did not return a structured final message"
        # A tool item can fail while Codex continues and finishes the turn.
        # Only a later final message AND explicit turn completion establish
        # recovery; turn/protocol errors below always remain failures.
        completed_indexes = [
            index
            for index, event in enumerate(events)
            if str(event.get("type") or "").lower() == "turn.completed"
        ]
        message_indexes = [
            index
            for index, event in enumerate(events)
            if str(event.get("type") or "").lower() == "item.completed"
            and isinstance(event.get("item"), dict)
            and str(event["item"].get("type") or "").lower() == "agent_message"
            and isinstance(event["item"].get("text"), str)
            and event["item"]["text"].strip()
        ]
        tool_failure_types: list[str] = []
        recovered_tool_failure_types: list[str] = []
        saw_agent_message = False
        for event_index, event in enumerate(events):
            event_type = str(event.get("type") or "").lower()
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            item_type = str(item.get("type") or "").lower()
            failed = event_type in {"error", "turn.failed", "turn_failure", "failed"}
            failed = failed or item_type in {"error", "failure", "failed"}
            failed = failed or str(item.get("status") or "").lower() in {"error", "failed", "failure"}
            top_level_error = "error" in event and event.get("error") is not None
            item_error = "error" in item and item.get("error") is not None
            failed = failed or top_level_error or item_error
            is_failed_tool_item = (
                failed
                and event_type == "item.completed"
                and item_type not in {"", "agent_message", "error", "failure", "failed"}
            )
            if is_failed_tool_item:
                tool_failure_types.append(item_type)
                self._last_cli_diagnostics["tool_failure_count"] = len(
                    tool_failure_types
                )
                self._last_cli_diagnostics["tool_failure_types"] = list(
                    tool_failure_types
                )
            recovered_tool_failure = (
                failed
                and event_type == "item.completed"
                and item_type in {"command_execution", "mcp_tool_call"}
                and not top_level_error
                and any(
                    event_index < message_index < completed_index
                    for message_index in message_indexes
                    for completed_index in completed_indexes
                )
            )
            if recovered_tool_failure:
                recovered_tool_failure_types.append(item_type)
                self._last_cli_diagnostics["recovered_tool_failure_count"] = len(
                    recovered_tool_failure_types
                )
                self._last_cli_diagnostics["recovered_tool_failure_types"] = list(
                    recovered_tool_failure_types
                )
                self._last_cli_diagnostics["recovered_command_failure_count"] = (
                    recovered_tool_failure_types.count("command_execution")
                )
                continue
            if failed:
                self._set_cli_diagnostic(
                    status="failed",
                    state="failed",
                    error_code="codex_turn_failed",
                )
                detail = (
                    event.get("message")
                    or (event.get("error") or {}).get("message")
                    if isinstance(event.get("error"), dict)
                    else event.get("error")
                ) or item.get("error") or "Codex CLI reported a failed turn"
                return f"Codex CLI reported a failed turn: {str(detail)[:1000]}"
            if (
                event_type == "item.completed"
                and item_type == "agent_message"
                and isinstance(item.get("text"), str)
                and item.get("text", "").strip()
            ):
                saw_agent_message = True
        if events and not saw_agent_message:
            self._set_cli_diagnostic(
                status="failed",
                state="failed",
                error_code="codex_missing_final_message",
            )
            return "Codex CLI did not return a final agent message"
        return None

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
        invocation_options: Optional[CLIInvocationOptions] = None,
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
        raw_prompt = bool(invocation_options.raw_prompt) if invocation_options is not None else False
        combined_prompt = prompt
        if system_context and not raw_prompt:
            combined_prompt = (
                f"{system_context}\n\nUser request:\n{prompt}" if prompt else system_context
            )
        if (
            not raw_prompt
            and str(native_session_action or "stateless") != "resume"
            and not disable_native_tools
        ):
            preamble = _AGENT_TEAM_CLI_PREAMBLE if _active_agent_team_cli.get() else _CODEX_ADAPTER_PREAMBLE
            combined_prompt = f"{preamble}\n\n{combined_prompt}"

        try:
            return super().execute_prompt(
                prompt if raw_prompt else "",
                cwd=cwd,
                timeout=timeout,
                extra_args=extra_args,
                system_context=(system_context if raw_prompt else combined_prompt),
                event_callback=event_callback,
                native_session_id=native_session_id,
                native_session_action=native_session_action,
                ephemeral=ephemeral,
                invocation_options=invocation_options,
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
