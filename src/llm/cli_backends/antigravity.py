"""
Antigravity CLI backend implementation.

Usage: agy [--dangerously-skip-permissions] [--model MODEL] -p "prompt"
Docs: https://antigravity.google/docs/cli-reference

Antigravity CLI does not expose runtime MCP flags; configure plugins and MCP
servers in Antigravity's native settings.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import (CLIBackendBase, CLIEventCallback, CLIInvocationOptions,
                   CLISessionCapabilities)

logger = logging.getLogger(__name__)


class AntigravityCLIBackend(CLIBackendBase):
    """Antigravity CLI backend implementation."""

    scoped_execution_delegate = True
    provider_state_root_supported = True

    prompt_stdin_supported = False
    direct_prompt_max_length = 24000
    # Agy's default --print-timeout=0 can block on upstream transport retries.
    # Keep a separate process deadline because older agy versions may ignore
    # the native print deadline on certain provider failures.
    _default_print_timeout_seconds = 300
    _process_timeout_grace_seconds = 20
    _ssh_session_env_names = ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")
    _native_sessions_available: Optional[bool] = None
    _failure_statuses = frozenset({
        "ERROR", "INVALID", "CANCELED", "CANCELLED", "INTERRUPTED",
        "FAILED", "FAILURE", "WAITING", "RUNNING",
    })
    _permission_denial_markers = (
        "permission denied",
        "permission_denied",
        "soft-denied",
        "soft denied",
        "approval required",
        "requires approval",
        "tool call rejected",
        "not permitted",
        "not approved",
        "was denied",
        "were denied by policy",
    )

    def __init__(self, model: Optional[str] = None):
        self._model = model
        self._active_add_dirs: List[str] = []
        self._active_print_timeout: Optional[str] = None
        super().__init__()

    def get_session_capabilities(self) -> CLISessionCapabilities:
        """Detect Antigravity's documented JSON conversation continuation."""

        if self.__class__._native_sessions_available is None:
            self.__class__._native_sessions_available = self.cli_help_contains(
                self._agy_bin(),
                ["--help"],
                "--conversation",
                "--output-format",
                "stream-json",
            )
        supported = bool(self.__class__._native_sessions_available)
        return CLISessionCapabilities(
            native_sessions=supported,
            supports_resume=supported,
            supports_follow_up=supported,
            # Agy allocates the id in the JSON envelope after the run.
            supports_explicit_session_id=False,
            fallback_to_stateless=False,
            requires_output_session_id=supported,
        )

    def extract_native_session_id(self, raw_output: str) -> Optional[str]:
        """Extract only Antigravity's explicit ``conversation_id`` field."""

        import json as _json

        for line in str(raw_output or "").splitlines():
            try:
                event = _json.loads(line)
            except (TypeError, _json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            value = event.get("conversation_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            result = event.get("result")
            if isinstance(result, dict):
                value = result.get("conversation_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return super().extract_native_session_id(raw_output)

    def validate_invocation_options(self, options: CLIInvocationOptions) -> None:
        super().validate_invocation_options(options)
        if options.provider_state_root is not None:
            expected = self._safe_resolve(Path.home() / ".gemini" / "antigravity-cli")
            supplied = self._safe_resolve(Path(options.provider_state_root).expanduser())
            if supplied != expected:
                raise ValueError(
                    "Antigravity CLI only accepts its native Windows user state root"
                )
        policy = options.effective_approval_policy
        if policy not in {"native_default", "standard", "accept_edits"}:
            raise ValueError(f"Antigravity CLI does not support approval policy: {policy}")
        if options.sandbox is not None and not isinstance(options.sandbox, (bool, str)):
            raise ValueError("Antigravity CLI sandbox must be a boolean or string")
        if isinstance(options.sandbox, str) and options.sandbox.strip().lower() not in {
            "native_default",
            "enabled",
            "disabled",
            "true",
            "false",
            "on",
            "off",
            "sandbox",
        }:
            raise ValueError(f"Antigravity CLI does not support sandbox mode: {options.sandbox}")

    @staticmethod
    def _is_transport_failure(message: str) -> bool:
        lower = str(message or "").lower()
        return any(marker in lower for marker in (
            "wsarecv", "wsasend", "dial tcp", "connection reset",
            "connection refused", "connection timed out", "i/o timeout",
            "context canceled", "context cancelled", "network is unreachable",
            "deadline exceeded",
        ))

    @classmethod
    def _is_permission_denial(cls, message: str) -> bool:
        lower = str(message or "").lower()
        return any(marker in lower for marker in cls._permission_denial_markers)

    @classmethod
    def _permission_denial_detail(cls, stdout: str, stderr: str) -> str:
        """Find an explicit CLI soft-deny without matching model response text."""

        for line in str(stderr or "").splitlines():
            if cls._is_permission_denial(line):
                return line.strip()

        # Durable-session Popen streams merge stderr into stdout. Scan only
        # non-JSON notice lines and structured error fields so an assistant
        # response that discusses permissions is not mistaken for a denial.
        for line in str(stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                if cls._is_permission_denial(line):
                    return line.strip()
                continue
            if not isinstance(event, dict):
                continue
            result = event.get("result") if isinstance(event.get("result"), dict) else event
            error = result.get("error") if isinstance(result, dict) else None
            if isinstance(error, str) and cls._is_permission_denial(error):
                return error.strip()
            response = result.get("response") if isinstance(result, dict) else None
            if isinstance(response, str) and re.search(
                r"\buser denied permission for\s+[a-z0-9_.-]+\b",
                response,
                flags=re.IGNORECASE,
            ):
                return "Antigravity CLI response reports an explicit user permission denial"
            # agy 1.4.3 reports headless auto-denials as SUCCESS with an empty
            # response and ``denied_actions`` (e.g. RunCommand under accept-edits).
            for denial_key in ("permission_denials", "denied_actions"):
                denials = result.get(denial_key) if isinstance(result, dict) else None
                if isinstance(denials, list) and denials:
                    return "Antigravity CLI result contains explicit permission denials"
            step = event.get("step_update")
            tool_info = step.get("tool_info") if isinstance(step, dict) else None
            if isinstance(tool_info, dict):
                tool_status = str(tool_info.get("status") or "").strip().lower()
                if tool_status in {"denied", "permission_denied", "rejected"}:
                    return "Antigravity CLI tool event reports an explicit permission denial"
                tool_error = tool_info.get("error") or tool_info.get("result")
                if isinstance(tool_error, str) and cls._is_permission_denial(tool_error):
                    return tool_error.strip()
            if str(event.get("event") or "").lower() in {
                "permission_denied", "tool_denied", "approval_required",
            }:
                return "Antigravity CLI emitted an explicit permission-denial event"
        return ""

    def _permission_denial_description(self) -> str:
        self._set_cli_diagnostic(
            status="failed", state="failed", error_code="agy_permission_denied",
        )
        return (
            "Antigravity CLI の既存権限設定で操作が拒否されました。"
            "権限変更や別経路での再実行は行っていません。"
        )

    @staticmethod
    def _safe_failure_text(message: str) -> str:
        # Do not return upstream URLs or authentication material to the chat.
        text = str(message or "").strip()
        text = re.sub(r"https?://\S+", "[endpoint]", text)
        text = re.sub(
            r"(?i)(bearer\s+|(?:token|secret|api[_-]?key)\s*[:=]\s*)\S+",
            r"\1[REDACTED]",
            text,
        )
        return text[:500]

    def _failure_description(self, detail: str, *, status: str = "ERROR") -> str:
        if self._is_transport_failure(detail):
            self._set_cli_diagnostic(
                status="failed", state="failed", error_code="agy_transport_failure",
            )
            return (
                "Antigravity CLI が通信切断またはtimeoutを報告しました"
                "（wsarecv/wsasend等）。この情報だけでは原因を特定できません。"
            )
        return f"Antigravity CLI returned status {status}" + (
            f": {self._safe_failure_text(detail)}" if detail else ""
        )

    def parse_error_output(self, stdout: str, stderr: str, exit_code: int) -> Optional[str]:
        """Classify merged Popen output and redact CLI transport details."""
        detail = str(stderr or "").strip()
        status = "ERROR"
        plain_lines: list[str] = []
        for line in str(stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                if line.strip():
                    plain_lines.append(line.strip())
                continue
            if not isinstance(event, dict):
                continue
            result = event.get("result") if isinstance(event.get("result"), dict) else event
            event_status = str(result.get("status") or "").strip().upper()
            if event_status:
                status = event_status
            error = result.get("error") if isinstance(result, dict) else None
            if isinstance(error, str) and error.strip():
                detail = error.strip()
        if not detail and plain_lines:
            detail = "\n".join(plain_lines)
        denial_detail = self._permission_denial_detail(stdout, stderr)
        if self._is_permission_denial(detail) or denial_detail:
            return self._permission_denial_description()
        if self._is_transport_failure(detail):
            return self._failure_description(detail, status=status)
        if detail:
            return f"Antigravity CLI failed (exit={exit_code}): {self._safe_failure_text(detail)}"
        return f"Antigravity CLI failed (exit={exit_code})"

    def _resolve_print_timeouts(self, timeout: Optional[int]) -> tuple[str, Optional[int]]:
        override = str(os.getenv("AGY_PRINT_TIMEOUT") or "").strip()
        if override:
            return override, timeout
        if timeout is None:
            seconds = self._default_print_timeout_seconds
            return f"{seconds}s", seconds + self._process_timeout_grace_seconds
        return f"{max(int(timeout) - 2, 1)}s", timeout

    def validate_success_output(self, raw_output: str, stderr: str) -> Optional[str]:
        """Require a terminal result for NDJSON, rejecting partial success."""

        import json as _json

        statuses: list[str] = []
        errors: list[str] = []
        saw_stream_event = False
        saw_terminal_result = False
        if self._permission_denial_detail(raw_output, stderr):
            return self._permission_denial_description()
        for line in str(raw_output or "").splitlines():
            try:
                event = _json.loads(line)
            except (TypeError, _json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "").lower()
            if event_name in {"init", "step_update"}:
                saw_stream_event = True
            if event_name == "result":
                saw_terminal_result = True
            result = event.get("result") if isinstance(event.get("result"), dict) else event
            status = result.get("status") if isinstance(result, dict) else None
            if status:
                statuses.append(str(status).strip().upper())
            error = result.get("error") if isinstance(result, dict) else None
            if error:
                errors.append(str(error).strip())
        if statuses and statuses[-1] in self._failure_statuses:
            status = statuses[-1]
            detail = errors[-1] if errors else str(stderr or "").strip()
            if status == "WAITING":
                self._set_cli_diagnostic(
                    status="waiting",
                    state="waiting_input",
                    error_code="agy_waiting_input",
                )
            else:
                self._set_cli_diagnostic(
                    status="failed",
                    state="failed",
                    error_code=f"agy_status_{status.lower()}",
                )
            return self._failure_description(detail, status=status)
        if saw_stream_event and not saw_terminal_result:
            self._set_cli_diagnostic(
                status="failed", state="failed", error_code="agy_result_missing",
            )
            return "Antigravity CLI が最終 result を返さず終了しました。途中出力を成功扱いしません。"
        return None

    def observe_effective_launch_config(self, raw_output: str, stderr: str) -> None:
        """Record Antigravity's structured init confirmation without IDs."""

        for line in str(raw_output or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or str(event.get("event") or "").lower() != "init":
                continue
            init = event.get("init") if isinstance(event.get("init"), dict) else event
            self._record_observed_launch_fields(
                cwd=init.get("cwd"),
                model=init.get("model"),
                permission_mode=init.get("permission_mode"),
            )
            return

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build an Antigravity CLI print-mode command."""
        # Always request typed result events.  Plain text can hide provider
        # ERROR and incomplete upstream turns as apparent success.
        cmd = [self._agy_bin(), "--output-format", "stream-json"]

        options = self.get_invocation_options()
        if options is not None:
            policy = options.effective_approval_policy
            # Native default preserves Antigravity's saved permission rules.
            # accept_edits only auto-approves file edits; it does not grant
            # shell or network access.
            if policy == "standard":
                cmd.extend(["--mode", "default"])
            elif policy == "accept_edits":
                cmd.extend(["--mode", "accept-edits"])
        elif self._truthy_env("AGY_AUTO_APPROVE", default=False):
            # Legacy callers can opt into broad approvals explicitly. Never
            # turn them on just because the environment variable is absent.
            cmd.append("--dangerously-skip-permissions")

        if options is not None:
            sandbox = options.sandbox
            sandbox_enabled = isinstance(sandbox, bool) and sandbox
            if isinstance(sandbox, str):
                sandbox_enabled = sandbox.strip().lower() in {"enabled", "true", "on", "sandbox"}
            if sandbox_enabled:
                cmd.append("--sandbox")
        elif self._truthy_env("AGY_SANDBOX", default=False):
            cmd.append("--sandbox")

        if options is not None:
            model = str(options.model or "").strip()
            effort = str(options.effective_effort or "").strip()
        else:
            model = (self._model or os.getenv("AGY_MODEL") or "").strip()
            effort = ""
        if model and model.lower() != "default":
            cmd.extend(["--model", model])

        if effort:
            cmd.extend(["--effort", effort])

        action = str(getattr(self, "_active_native_session_action", "stateless"))
        session_id = str(getattr(self, "_active_native_session_id", "") or "").strip()
        if action in {"start", "resume"}:
            # Session actions use the same structured output contract.
            if action == "resume":
                if not session_id:
                    raise ValueError("Antigravity CLI native resume requires a conversation id")
                cmd.extend(["--conversation", session_id])

        # A host log path would be unreachable (and potentially outside the
        # repository) inside the scoped WSL namespace.  Keep it only for the
        # ordinary unscoped desktop path.
        log_file = (
            None if self._active_run_scope() is not None else os.getenv("AGY_LOG_FILE")
        )
        if log_file:
            cmd.extend(["--log-file", str(log_file)])

        print_timeout = self._active_print_timeout or os.getenv("AGY_PRINT_TIMEOUT")
        if print_timeout:
            cmd.extend(["--print-timeout", str(print_timeout).strip()])

        for add_dir in self._active_add_dirs:
            cmd.extend(["--add-dir", add_dir])

        cmd.extend(["-p", prompt])
        return cmd

    def get_provider_name(self) -> str:
        return "Antigravity CLI"

    def get_subprocess_env(self) -> Dict[str, str]:
        """Preserve Antigravity's native user profile for saved auth/settings."""
        env = super().get_subprocess_env()
        # The shared child allowlist omits user directories. Antigravity CLI
        # resolves its existing settings and keyring integration from these
        # native profile paths, so pass only the path variables it needs.
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            value = os.getenv(name)
            if value:
                env[name] = value
        if os.name == "nt":
            profile = env.get("USERPROFILE") or str(Path.home())
            env.setdefault("USERPROFILE", profile)
            env.setdefault("HOME", profile)
        else:
            env.setdefault("HOME", str(Path.home()))

        if self._truthy_env("AGY_PRESERVE_SSH_ENV", default=False):
            return env

        removed = [name for name in self._ssh_session_env_names if name in env]
        for name in removed:
            env.pop(name, None)

        if removed:
            logger.info(
                "[Antigravity CLI] Removed SSH session markers for subprocess auth: %s",
                ", ".join(removed),
            )
        return env

    def parse_output(self, raw_output: str) -> str:
        """Parse text, JSON, and streaming JSON headless output."""
        output = str(raw_output or "").strip()
        if not output:
            return output

        responses: list[str] = []
        deltas: list[str] = []
        parsed_any = False
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            parsed_any = True
            result = event.get("result") if isinstance(event.get("result"), dict) else event
            response = result.get("response") if isinstance(result, dict) else None
            if isinstance(response, str) and response.strip():
                responses.append(response.strip())
            step = event.get("step_update")
            if isinstance(step, dict):
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    deltas.append(delta)
        if responses:
            return responses[-1]
        if deltas:
            return "".join(deltas).strip()
        if parsed_any:
            return ""
        return output

    def handle_stream_output_line(
        self,
        line: str,
        event_callback: CLIEventCallback,
    ) -> None:
        """Expose explicit Antigravity status/text events to the caller."""

        try:
            event = json.loads(str(line or "").strip())
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        event_name = str(event.get("event") or event.get("type") or "").strip().lower()
        if event_name == "init":
            event_callback(
                "status_update",
                {"status": "antigravity_session_started", "event": event},
            )
            return
        if event_name == "result":
            result = event.get("result") if isinstance(event.get("result"), dict) else event
            event_callback(
                "status_update",
                {"status": str(result.get("status") or "result").lower(), "event": event},
            )
            return
        step = event.get("step_update")
        if isinstance(step, dict):
            text = step.get("text_delta")
            if isinstance(text, str) and text:
                event_callback(
                    "status_update",
                    {"status": "antigravity_text_delta", "message": text},
                )

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
        invocation_options: Optional[CLIInvocationOptions] = None,
    ) -> Tuple[bool, str]:
        """Execute Antigravity print mode.

        Antigravity CLI's -p flag requires the full prompt as an argument and
        does not accept stdin as the prompt source. Very long prompts are
        written to a workspace temp file and the CLI is asked to read it.
        """
        options = invocation_options
        if options is not None and options.cwd is not None:
            cwd = Path(options.cwd)
        raw_prompt = bool(options.raw_prompt) if options is not None else False
        combined_prompt = (
            prompt if raw_prompt else self._combine_prompt(prompt, system_context)
        )
        prompt_cleanup: Optional[Callable[[], None]] = None

        scoped = self._active_run_scope()
        print_timeout, effective_timeout = self._resolve_print_timeouts(timeout)
        if scoped is not None:
            # Never create the historical profile/temp workspace for a trusted
            # repository worker: those paths are outside the WSL mount and
            # would make the provider's ``--add-dir`` escape ambiguous.
            try:
                scoped_cwd = scoped.assert_command_cwd_allowed(
                    scoped.canonical_root if cwd is None else cwd
                )
            except Exception as exc:
                return False, f"Antigravity CLI scoped execution denied: {exc}"
            self._active_print_timeout = print_timeout
            self._active_add_dirs = []
            try:
                return super().execute_prompt(
                    combined_prompt,
                    cwd=scoped_cwd,
                    timeout=effective_timeout,
                    extra_args=extra_args,
                    system_context=None,
                    event_callback=event_callback,
                    native_session_id=native_session_id,
                    native_session_action=native_session_action,
                    ephemeral=ephemeral,
                    invocation_options=options,
                )
            finally:
                self._active_print_timeout = None
                self._active_add_dirs = []

        if len(combined_prompt) > self.direct_prompt_max_length and options is None:
            combined_prompt, prompt_cleanup = self._prompt_file_instruction(
                combined_prompt,
                cwd,
            )

        if options is not None:
            execution_cwd, add_dirs = cwd, []
        else:
            execution_cwd, add_dirs = self._execution_workspace(cwd)
        started_at = time.time()
        self._active_print_timeout = print_timeout
        self._active_add_dirs = add_dirs
        try:
            success, output = super().execute_prompt(
                combined_prompt,
                cwd=execution_cwd,
                timeout=effective_timeout,
                extra_args=extra_args,
                system_context=None,
                event_callback=event_callback,
                native_session_id=native_session_id,
                native_session_action=native_session_action,
                ephemeral=ephemeral,
                invocation_options=options,
            )
            if success and not output.strip():
                recovered_output = self._recover_output_from_transcript(started_at)
                if recovered_output:
                    logger.info(
                        "[Antigravity CLI] Recovered empty print-mode stdout from transcript"
                    )
                    return True, recovered_output
                return False, self._empty_output_diagnostic(started_at)
            return success, output
        finally:
            self._active_print_timeout = None
            self._active_add_dirs = []
            if prompt_cleanup:
                prompt_cleanup()

    def prepare_image_attachment(
        self, image_data: Dict[str, Any], cwd: Optional[Path] = None
    ) -> Optional[Tuple[str, Callable[[], None]]]:
        """Save a base64 image to a temp file and reference the path in prompt."""
        data_url = image_data.get("data", "")
        if not data_url:
            return None

        try:
            if data_url.startswith("data:"):
                header, encoded = data_url.split(",", 1)
                mime_type = image_data.get("mimeType") or header.split(";")[0].split(":")[1]
            else:
                encoded = data_url
                mime_type = image_data.get("mimeType", "image/png")

            image_bytes = base64.b64decode(encoded)
            ext = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
            }.get(mime_type, ".png")

            tmp_dir = self._temp_dir(cwd)
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=str(tmp_dir))
            tmp.write(image_bytes)
            tmp.close()
            tmp_path = Path(tmp.name)

            logger.info(
                "[Antigravity CLI] Saved image attachment: %s (%s, %s bytes)",
                tmp_path,
                mime_type,
                len(image_bytes),
            )

            def cleanup() -> None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning(
                        "[Antigravity CLI] Failed to remove temp image file: %s",
                        exc,
                    )

            return (f"\n\nAttached image file: {tmp_path}", cleanup)

        except Exception as exc:
            logger.warning("[Antigravity CLI] Image preparation failed: %s", exc)
            return None

    def prepare_audio_attachment(
        self, audio_data: Dict[str, Any], cwd: Optional[Path] = None
    ) -> Optional[Tuple[str, Callable[[], None]]]:
        """Save audio as a temporary file so Antigravity can inspect it by path."""
        data_url = str(audio_data.get("data") or audio_data.get("dataUrl") or "")
        if not data_url:
            return None
        try:
            header, encoded = data_url.split(",", 1) if data_url.startswith("data:") else ("", data_url)
            mime_type = str(audio_data.get("mimeType") or header.split(";", 1)[0].removeprefix("data:") or "audio/wav")
            suffix = {
                "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "audio/ogg": ".ogg", "audio/webm": ".webm",
            }.get(mime_type, ".audio")
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=str(self._temp_dir(cwd)))
            tmp.write(base64.b64decode(encoded))
            tmp.close()
            path = Path(tmp.name)

            def cleanup() -> None:
                try:
                    path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("[Antigravity CLI] Failed to remove temp audio file: %s", exc)

            return (f"\n\nAttached audio file: {path}", cleanup)
        except Exception as exc:
            logger.warning("[Antigravity CLI] Audio preparation failed: %s", exc)
            return None

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Antigravity CLI does not support runtime MCP arguments."""
        if mcp_servers:
            logger.info(
                "[Antigravity CLI] %s MCP server(s) in config.yaml. "
                "Configure them in Antigravity CLI native plugin/settings files.",
                len(mcp_servers),
            )
        return []

    def _agy_bin(self) -> str:
        configured = (
            os.getenv("AGY_BIN")
            or os.getenv("ANTIGRAVITY_BIN")
            or os.getenv("ANTIGRAVITY_CLI_BIN")
        )
        if configured:
            return configured

        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if candidate.exists():
                return str(candidate)

        return "agy"

    def _execution_workspace(
        self,
        cwd: Optional[Path],
    ) -> Tuple[Optional[Path], List[str]]:
        """Run print mode from a stable workspace and add the project separately.

        On Windows, Antigravity print mode can fail to write its transcript when
        the process cwd is outside the user profile, which makes stdout empty
        even after model generation succeeds. Use the profile workspace for the
        process and grant access to the requested project directory via
        ``--add-dir``.
        """
        if cwd is None or self._truthy_env("AGY_USE_CALLER_CWD", default=False):
            return cwd, []

        requested_cwd = self._safe_resolve(Path(cwd))
        execution_cwd = self._configured_workspace_cwd()

        if execution_cwd is None:
            execution_cwd = self._safe_resolve(Path.home())

        add_dirs: List[str] = []
        if not self._path_contains(execution_cwd, requested_cwd):
            add_dirs.append(str(requested_cwd))

        if execution_cwd != requested_cwd:
            logger.info(
                "[Antigravity CLI] Running from workspace %s with add-dir %s",
                execution_cwd,
                requested_cwd,
            )
        return execution_cwd, add_dirs

    def _configured_workspace_cwd(self) -> Optional[Path]:
        configured = (
            os.getenv("AGY_WORKSPACE_CWD")
            or os.getenv("ANTIGRAVITY_WORKSPACE_CWD")
        )
        if not configured:
            return None

        workspace = self._safe_resolve(Path(configured).expanduser())
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(
                "[Antigravity CLI] Failed to create configured workspace %s: %s",
                workspace,
                exc,
            )
            return None
        return workspace

    def _safe_resolve(self, path: Path) -> Path:
        try:
            return path.expanduser().resolve()
        except Exception:
            return path.expanduser().absolute()

    def _path_contains(self, parent: Path, child: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def _app_data_dir(self) -> Path:
        configured = (
            os.getenv("AGY_APP_DATA_DIR")
            or os.getenv("ANTIGRAVITY_CLI_APP_DATA_DIR")
        )
        if configured:
            return self._safe_resolve(Path(configured).expanduser())
        return Path.home() / ".gemini" / "antigravity-cli"

    def _recover_output_from_transcript(self, started_at: float) -> Optional[str]:
        transcript = self._latest_transcript_since(started_at)
        if transcript is None:
            return None

        contents: List[str] = []
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("source") != "MODEL" or item.get("status") != "DONE":
                        continue
                    content = item.get("content")
                    if isinstance(content, str) and content.strip():
                        contents.append(content.strip())
        except OSError as exc:
            logger.warning(
                "[Antigravity CLI] Failed to read transcript fallback %s: %s",
                transcript,
                exc,
            )
            return None

        if not contents:
            return None
        return contents[-1]

    def _latest_transcript_since(self, started_at: float) -> Optional[Path]:
        brain_dir = self._app_data_dir() / "brain"
        if not brain_dir.is_dir():
            return None

        threshold = max(started_at - 5.0, 0.0)
        candidates: List[Tuple[float, Path]] = []
        try:
            for transcript in brain_dir.rglob("transcript.jsonl"):
                try:
                    stat = transcript.stat()
                except OSError:
                    continue
                if stat.st_size > 0 and stat.st_mtime >= threshold:
                    candidates.append((stat.st_mtime, transcript))
        except OSError as exc:
            logger.warning(
                "[Antigravity CLI] Failed to scan transcript fallback directory: %s",
                exc,
            )
            return None

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _combine_prompt(self, prompt: str, system_context: Optional[str]) -> str:
        if not system_context:
            return prompt
        if not prompt:
            return system_context
        return f"{system_context}\n\nUser request:\n{prompt}"

    def _prompt_file_instruction(
        self,
        prompt: str,
        cwd: Optional[Path],
    ) -> Tuple[str, Callable[[], None]]:
        tmp_dir = self._temp_dir(cwd)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="aoitalk-agy-prompt-",
            encoding="utf-8",
            delete=False,
            dir=str(tmp_dir),
        )
        tmp.write(prompt)
        tmp.close()
        tmp_path = Path(tmp.name)

        def cleanup() -> None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(
                    "[Antigravity CLI] Failed to remove temp prompt file: %s",
                    exc,
                )

        instruction = (
            "Read the full AoiTalk prompt from this UTF-8 file and answer it: "
            f"{tmp_path}"
        )
        logger.info("[Antigravity CLI] Prompt moved to temp file: %s", tmp_path)
        return instruction, cleanup

    def _empty_output_diagnostic(self, started_at: Optional[float] = None) -> str:
        if self._recent_log_indicates_auth_unavailable(started_at):
            return (
                "Antigravity CLI returned no output and recent CLI logs indicate "
                "the saved Windows keyring authentication is unavailable from the "
                "current launch context. Start AoiTalk from the logged-in desktop "
                "session, or authenticate Antigravity in the same non-interactive "
                "context."
            )

        return (
            "Antigravity CLI returned no output from print mode. "
            "Update Antigravity CLI and confirm `agy -p \"hello\"` prints text "
            "from a non-interactive shell."
        )

    def _recent_log_indicates_auth_unavailable(
        self,
        started_at: Optional[float],
    ) -> bool:
        log_dir = self._app_data_dir() / "log"
        if not log_dir.is_dir():
            return False

        threshold = max((started_at or time.time()) - 5.0, 0.0)
        try:
            logs = sorted(
                (
                    path
                    for path in log_dir.glob("cli-*.log")
                    if path.is_file()
                    and path.stat().st_mtime >= threshold
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return False

        failure_markers = (
            "Failed to load token from keyring",
            "A specified logon session does not exist",
            "Print mode: auth timed out",
            "You are not logged into Antigravity",
        )
        success_markers = (
            "keyringAuth: loaded token",
            "ChainedAuth: authenticated",
            "OAuth: authenticated successfully",
            "Print mode: silent auth succeeded",
            "applyAuthResult:",
            "streamGenerateContent",
        )
        for log_path in logs[:3]:
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(marker in text for marker in success_markers):
                continue
            if any(marker in text for marker in failure_markers):
                return True
        return False

    def _temp_dir(self, cwd: Optional[Path]) -> Path:
        if cwd:
            tmp_dir = Path(cwd) / "cache" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            return tmp_dir
        return Path(tempfile.gettempdir())

    def _truthy_env(self, name: str, *, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}
