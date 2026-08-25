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
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import CLIBackendBase, CLIEventCallback

logger = logging.getLogger(__name__)


class AntigravityCLIBackend(CLIBackendBase):
    """Antigravity CLI backend implementation."""

    scoped_execution_delegate = True

    prompt_stdin_supported = False
    direct_prompt_max_length = 24000
    _ssh_session_env_names = ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")

    def __init__(self, model: Optional[str] = None):
        self._model = model
        self._active_add_dirs: List[str] = []
        self._active_print_timeout: Optional[str] = None
        super().__init__()

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build an Antigravity CLI print-mode command."""
        cmd = [self._agy_bin()]

        if self._truthy_env("AGY_AUTO_APPROVE", default=True):
            cmd.append("--dangerously-skip-permissions")

        if self._truthy_env("AGY_SANDBOX", default=False):
            cmd.append("--sandbox")

        model = (self._model or os.getenv("AGY_MODEL") or "").strip()
        if model and model.lower() != "default":
            cmd.extend(["--model", model])

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
        """Use normal desktop auth even when AoiTalk was launched over SSH."""
        env = super().get_subprocess_env()
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
        """Antigravity print mode returns plain text."""
        return raw_output.strip()

    def execute_prompt(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
        extra_args: Optional[List[str]] = None,
        system_context: Optional[str] = None,
        event_callback: Optional[CLIEventCallback] = None,
    ) -> Tuple[bool, str]:
        """Execute Antigravity print mode.

        Antigravity CLI's -p flag requires the full prompt as an argument and
        does not accept stdin as the prompt source. Very long prompts are
        written to a workspace temp file and the CLI is asked to read it.
        """
        combined_prompt = self._combine_prompt(prompt, system_context)
        prompt_cleanup: Optional[Callable[[], None]] = None

        scoped = self._active_run_scope()
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
            self._active_print_timeout = os.getenv("AGY_PRINT_TIMEOUT") or (
                f"{max(int(timeout) - 2, 1)}s" if timeout is not None else None
            )
            self._active_add_dirs = []
            try:
                return super().execute_prompt(
                    combined_prompt,
                    cwd=scoped_cwd,
                    timeout=timeout,
                    extra_args=extra_args,
                    system_context=None,
                    event_callback=event_callback,
                )
            finally:
                self._active_print_timeout = None
                self._active_add_dirs = []

        if len(combined_prompt) > self.direct_prompt_max_length:
            combined_prompt, prompt_cleanup = self._prompt_file_instruction(
                combined_prompt,
                cwd,
            )

        execution_cwd, add_dirs = self._execution_workspace(cwd)
        started_at = time.time()
        self._active_print_timeout = os.getenv("AGY_PRINT_TIMEOUT") or (
            f"{max(int(timeout) - 2, 1)}s" if timeout is not None else None
        )
        self._active_add_dirs = add_dirs
        try:
            success, output = super().execute_prompt(
                combined_prompt,
                cwd=execution_cwd,
                timeout=timeout,
                extra_args=extra_args,
                system_context=None,
                event_callback=event_callback,
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
