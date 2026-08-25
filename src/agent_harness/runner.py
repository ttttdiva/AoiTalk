"""Agent runner abstractions for the AoiTalk harness."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AgentHarnessSettings
from .models import HarnessEventCallback, RunResult, WorkItem
from ..utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)


class AgentRunner:
    async def run(
        self,
        *,
        work_item: WorkItem,
        workspace: Path,
        prompt: str,
        attempt: int | None,
        on_event: HarnessEventCallback | None = None,
    ) -> RunResult:
        raise NotImplementedError


class CodexExecRunner(AgentRunner):
    """Run Codex CLI as a real repository agent in the task worktree."""

    def __init__(self, settings: AgentHarnessSettings):
        self.settings = settings

    async def run(
        self,
        *,
        work_item: WorkItem,
        workspace: Path,
        prompt: str,
        attempt: int | None,
        on_event: HarnessEventCallback | None = None,
    ) -> RunResult:
        active_scope = _active_run_scope()
        command_workspace = (
            _scoped_workspace_path(active_scope, workspace)
            if active_scope is not None
            else None
        )
        cmd = self._command(
            workspace,
            prompt,
            resolve_bin=active_scope is None,
            command_workspace=command_workspace,
        )
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "codex_exec_started",
                    "message": {
                        "work_item": work_item.identifier,
                        "attempt": attempt,
                        "workspace": str(workspace),
                    },
                },
            )

        if active_scope is not None:
            return await _run_codex_scoped_process(
                cmd,
                scope=active_scope,
                cwd=workspace,
                on_event=on_event,
            )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"}),
        )

        assert process.stdout is not None
        final_message = ""
        error_message = ""
        input_tokens = output_tokens = total_tokens = 0
        provider_session_id = None
        raw_tail: list[str] = []

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if raw:
                raw_tail.append(raw)
                raw_tail = raw_tail[-20:]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                if on_event and raw:
                    await _emit(on_event, {"event": "codex_output", "message": raw})
                continue

            # Codex has used a few different names for its native continuation
            # handle over time.  Normalize all of them to the harness-level
            # provider_session_id before emitting the event, while retaining
            # the raw payload in ``message`` for diagnostics/consumers that
            # understand provider-specific event shapes.
            provider_session_id = provider_session_id or _extract_provider_session_id(payload)
            event_type = str(payload.get("type") or payload.get("method") or "codex_event")
            if on_event:
                event: dict[str, Any] = {"event": event_type, "message": payload}
                if provider_session_id:
                    event["provider_session_id"] = provider_session_id
                await _emit(on_event, event)

            usage = _extract_usage(payload)
            if usage:
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)

            message = _extract_agent_message(payload)
            if message:
                final_message = message

            error = _extract_error(payload)
            if error:
                error_message = error

        return_code = await process.wait()
        if return_code == 0:
            return RunResult(
                success=True,
                message=final_message or "\n".join(raw_tail),
                provider_session_id=provider_session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )

        message = error_message or "\n".join(raw_tail) or f"codex exec failed: {return_code}"
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "codex_exec_failed",
                    "message": {"exit_code": return_code, "error": message},
                    **(
                        {"provider_session_id": provider_session_id}
                        if provider_session_id
                        else {}
                    ),
                },
            )
        return RunResult(
            success=False,
            message=message,
            provider_session_id=provider_session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def _command(
        self,
        workspace: Path,
        prompt: str,
        *,
        resolve_bin: bool = True,
        command_workspace: str | Path | None = None,
    ) -> list[str]:
        active_scope = _active_run_scope()
        if active_scope is not None:
            # Keep this helper safe even when an integration calls it directly
            # rather than going through ``run``.
            resolve_bin = False
            if command_workspace is None:
                command_workspace = _scoped_workspace_path(active_scope, workspace)
        configured_bin = self.settings.codex.bin_path
        bin_path = (
            (shutil.which(configured_bin) or configured_bin)
            if resolve_bin
            else configured_bin
        )
        cmd = [
            bin_path,
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(command_workspace if command_workspace is not None else workspace),
        ]
        if self.settings.codex.model:
            cmd.extend(["--model", self.settings.codex.model])
        if self.settings.codex.reasoning_effort:
            cmd.extend(
                [
                    "-c",
                    f'model_reasoning_effort="{self.settings.codex.reasoning_effort}"',
                ]
            )
        if self.settings.codex.approval_policy:
            cmd.extend(["-c", f'approval_policy="{self.settings.codex.approval_policy}"'])
        if self.settings.codex.exec_sandbox:
            cmd.extend(["--sandbox", self.settings.codex.exec_sandbox])
        cmd.append(prompt)
        return cmd


class ClaudeCodeRunner(AgentRunner):
    """Run Claude Code CLI as a repository agent in the task worktree."""

    def __init__(self, settings: AgentHarnessSettings):
        self.settings = settings

    async def run(
        self,
        *,
        work_item: WorkItem,
        workspace: Path,
        prompt: str,
        attempt: int | None,
        on_event: HarnessEventCallback | None = None,
    ) -> RunResult:
        active_scope = _active_run_scope()
        cmd = self._command(prompt, resolve_bin=active_scope is None)
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "claude_code_started",
                    "message": {
                        "work_item": work_item.identifier,
                        "attempt": attempt,
                        "workspace": str(workspace),
                    },
                },
            )
        if active_scope is not None:
            return await _run_plain_process_scoped(
                cmd,
                scope=active_scope,
                cwd=workspace,
                event_name="claude_output",
                failure_event="claude_code_failed",
                on_event=on_event,
            )
        return await _run_plain_process(
            cmd,
            cwd=workspace,
            event_name="claude_output",
            failure_event="claude_code_failed",
            on_event=on_event,
        )

    def _command(self, prompt: str, *, resolve_bin: bool = True) -> list[str]:
        if _active_run_scope() is not None:
            resolve_bin = False
        configured_bin = self.settings.claude.bin_path
        bin_path = (
            (shutil.which(configured_bin) or configured_bin)
            if resolve_bin
            else configured_bin
        )
        cmd = [bin_path, "-p"]
        if self.settings.claude.model:
            cmd.extend(["--model", self.settings.claude.model])
        if self.settings.claude.reasoning_effort:
            cmd.extend(["--effort", self.settings.claude.reasoning_effort])
        cmd.append(prompt)
        return cmd


class CustomCommandRunner(AgentRunner):
    """Run a configured command as an Agent Team work runner."""

    def __init__(self, settings: AgentHarnessSettings):
        self.settings = settings

    async def run(
        self,
        *,
        work_item: WorkItem,
        workspace: Path,
        prompt: str,
        attempt: int | None,
        on_event: HarnessEventCallback | None = None,
    ) -> RunResult:
        active_scope = _active_run_scope()
        cmd = self._command(
            work_item=work_item,
            workspace=workspace,
            prompt=prompt,
            workspace_value=(
                _scoped_workspace_path(active_scope, workspace)
                if active_scope is not None
                else None
            ),
        )
        if not cmd:
            return RunResult(success=False, message="custom_command runner is not configured")
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "custom_agent_started",
                    "message": {
                        "work_item": work_item.identifier,
                        "attempt": attempt,
                        "workspace": str(workspace),
                    },
                },
            )
        if active_scope is not None:
            return await _run_plain_process_scoped(
                cmd,
                scope=active_scope,
                cwd=workspace,
                event_name="custom_agent_output",
                failure_event="custom_agent_failed",
                on_event=on_event,
            )
        return await _run_plain_process(
            cmd,
            cwd=workspace,
            event_name="custom_agent_output",
            failure_event="custom_agent_failed",
            on_event=on_event,
        )

    def _command(
        self,
        *,
        work_item: WorkItem,
        workspace: Path,
        prompt: str,
        workspace_value: str | Path | None = None,
    ) -> list[str]:
        active_scope = _active_run_scope()
        if active_scope is not None and workspace_value is None:
            workspace_value = _scoped_workspace_path(active_scope, workspace)
        command = str(self.settings.custom_command.command or "").strip()
        if not command:
            return []
        values = {
            "prompt": prompt,
            "workspace": str(
                workspace if workspace_value is None else workspace_value
            ),
            "work_item": work_item.identifier,
        }
        parts = shlex.split(command, posix=os.name != "nt")
        args = [str(arg).format(**values) for arg in self.settings.custom_command.args]
        if "{prompt}" not in command and not any("{prompt}" in arg for arg in args):
            args.append(prompt)
        return [part.format(**values) for part in parts] + args


def build_runner(settings: AgentHarnessSettings) -> AgentRunner:
    runner_name = settings.codex.runner.strip().lower()
    if runner_name in {"codex_exec", "codex_cli"}:
        return CodexExecRunner(settings)
    if runner_name in {"claude_code", "claude_cli"}:
        return ClaudeCodeRunner(settings)
    if runner_name == "custom_command":
        return CustomCommandRunner(settings)
    raise ValueError(f"Unsupported agent harness runner: {settings.codex.runner}")


async def _emit(on_event: HarnessEventCallback, event: dict[str, Any]) -> None:
    maybe = on_event(event)
    if asyncio.iscoroutine(maybe):
        await maybe


def _active_run_scope() -> Any | None:
    """Return the request-local run scope without changing legacy callers.

    Agent Harness predates the run-scope contract and is also used by ordinary
    background automation.  Importing the scope lazily keeps that historical
    path intact while making an explicitly bound scope authoritative for every
    child process launched by this module.
    """

    try:
        from ..security.agent_run_scope import get_current_run_scope
    except ImportError:  # pragma: no cover - optional/stripped runtime builds
        return None
    # Do not swallow an unexpected ContextVar/runtime failure: treating that
    # as "no scope" would silently reopen the host subprocess fallback.
    return get_current_run_scope()


def _scoped_backend(scope: Any, cwd: Path) -> tuple[Any, Path] | str:
    """Validate the concrete WSL2+bwrap seam before spawning a child.

    ``AgentRunScope`` is a context authority, not a hint.  Once present, a
    runner must either use a verified file-scoped backend or deny execution;
    it must never fall back to ``asyncio.create_subprocess_exec``.
    """

    try:
        from ..security.agent_run_scope import AgentRunScope
        from ..security.wsl_bwrap_backend import (
            WslBwrapError,
            get_wsl_bwrap_backend,
        )
    except Exception as exc:  # pragma: no cover - defensive stripped builds
        return f"scoped harness execution denied: sandbox backend unavailable ({exc})"

    if not isinstance(scope, AgentRunScope):
        return "scoped harness execution denied: invalid AgentRunScope"

    try:
        safe_cwd = scope.assert_command_cwd_allowed(cwd)
        backend = get_wsl_bwrap_backend()
        if not bool(getattr(backend, "file_scoped", False)):
            raise WslBwrapError("configured backend is not file-scoped")
        is_available = getattr(backend, "is_available", None)
        if not callable(is_available) or not bool(is_available()):
            raise WslBwrapError(
                "file-scoped WSL2/bubblewrap backend is unavailable"
            )
        spawn = getattr(backend, "spawn", None)
        if not callable(spawn):
            raise WslBwrapError("configured backend cannot spawn scoped processes")
        return backend, Path(safe_cwd)
    except Exception as exc:
        return f"scoped harness execution denied: {exc}"


def _scoped_workspace_path(scope: Any | None, workspace: Path) -> str | None:
    """Return the mounted POSIX workspace path for custom/Codex arguments.

    A configured custom command may interpolate ``{workspace}``.  Passing the
    host path through to WSL is both unusable and an accidental disclosure of
    a path outside the mounted namespace, so map it to the backend's stable
    ``/workspace`` mount while retaining the unscoped legacy value.
    """

    if scope is None:
        return None
    try:
        safe_cwd = Path(scope.assert_command_cwd_allowed(Path(workspace)))
        relative = safe_cwd.relative_to(Path(scope.canonical_root))
        return str(PurePosixPath("/workspace", *relative.parts))
    except Exception:
        # Do not turn a mapping failure into a host-path fallback.  The actual
        # backend validation remains fail-closed and reports the reason.
        return "/workspace"


async def _spawn_scoped_process(
    cmd: list[str],
    *,
    scope: Any,
    cwd: Path,
) -> tuple[Any | None, str | None]:
    """Spawn one child through the verified WSL+bwrap backend only."""

    prepared = _scoped_backend(scope, cwd)
    if isinstance(prepared, str):
        return None, prepared
    backend, safe_cwd = prepared
    command = shlex.join(str(part) for part in cmd)
    env = build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"})
    try:
        process = backend.spawn(
            scope,
            command,
            cwd=safe_cwd,
            shell="bash",
            env=env,
            popen_kwargs={
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": False,
            },
        )
    except Exception as exc:
        return None, f"scoped harness execution denied: {exc}"
    if process is None or getattr(process, "stdout", None) is None:
        return None, "scoped harness execution denied: backend returned no process stream"
    return process, None


async def _read_process_line(process: Any) -> bytes | str:
    """Read a backend stream without blocking the asyncio event loop."""

    stream = getattr(process, "stdout", None)
    if stream is None:
        return b""
    # WslBwrapBackend owns a synchronous ``subprocess.Popen``.  Test doubles
    # and legacy adapters may expose an async readline; support both without
    # changing the host subprocess path.
    value = await asyncio.to_thread(stream.readline)
    if inspect.isawaitable(value):
        value = await value
    return value


async def _wait_scoped_process(process: Any) -> int:
    waiter = getattr(process, "wait", None)
    if not callable(waiter):
        return int(getattr(process, "returncode", -1) or -1)
    result = await asyncio.to_thread(waiter)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        result = getattr(process, "returncode", 0)
    return int(result)


def _terminate_scoped_process(process: Any) -> None:
    """Terminate a backend-owned process tree on cancellation/error."""

    try:
        if callable(getattr(process, "poll", None)) and process.poll() is not None:
            return
    except Exception:
        pass
    # Lightweight test/adaptor processes may not expose a host PID.  Avoid
    # sending them through the generic Windows tree-kill grace periods.
    if not getattr(process, "pid", None):
        for name in ("kill", "terminate"):
            try:
                method = getattr(process, name, None)
                if callable(method):
                    method()
                    return
            except Exception:
                continue
        return
    try:
        from ..tools.os_operations.command_executor import terminate_process_tree

        terminate_process_tree(process)
        return
    except Exception:
        pass
    for name in ("kill", "terminate"):
        try:
            method = getattr(process, name, None)
            if callable(method):
                method()
                return
        except Exception:
            continue


async def _run_plain_process_scoped(
    cmd: list[str],
    *,
    scope: Any,
    cwd: Path,
    event_name: str,
    failure_event: str,
    on_event: HarnessEventCallback | None = None,
) -> RunResult:
    """Run Claude/custom harness output through WSL2+bwrap."""

    process, error = await _spawn_scoped_process(cmd, scope=scope, cwd=cwd)
    if error:
        if on_event:
            await _emit(
                on_event,
                {
                    "event": failure_event,
                    "message": {"exit_code": None, "error": error},
                },
            )
        return RunResult(success=False, message=error)

    output_lines: list[str] = []
    try:
        while True:
            line = await _read_process_line(process)
            if line in (b"", "", None):
                break
            if isinstance(line, bytes):
                text = line.decode("utf-8", errors="replace").rstrip()
            else:
                text = str(line).rstrip()
            if not text:
                continue
            output_lines.append(text)
            output_lines = output_lines[-200:]
            if on_event:
                await _emit(on_event, {"event": event_name, "message": text})
        return_code = await _wait_scoped_process(process)
    except asyncio.CancelledError:
        await asyncio.to_thread(_terminate_scoped_process, process)
        raise
    except Exception as exc:
        await asyncio.to_thread(_terminate_scoped_process, process)
        error = f"scoped harness execution failed: {exc}"
        if on_event:
            await _emit(
                on_event,
                {
                    "event": failure_event,
                    "message": {"exit_code": None, "error": error},
                },
            )
        return RunResult(success=False, message=error)

    message = "\n".join(output_lines).strip()
    if return_code == 0:
        return RunResult(success=True, message=message)
    if on_event:
        await _emit(
            on_event,
            {
                "event": failure_event,
                "message": {"exit_code": return_code, "error": message},
            },
        )
    return RunResult(success=False, message=message or f"runner failed: {return_code}")


async def _run_codex_scoped_process(
    cmd: list[str],
    *,
    scope: Any,
    cwd: Path,
    on_event: HarnessEventCallback | None = None,
) -> RunResult:
    """Run Codex JSONL output through WSL2+bwrap without host subprocesses."""

    process, error = await _spawn_scoped_process(cmd, scope=scope, cwd=cwd)
    if error:
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "codex_exec_failed",
                    "message": {"exit_code": None, "error": error},
                },
            )
        return RunResult(success=False, message=error)

    final_message = ""
    error_message = ""
    input_tokens = output_tokens = total_tokens = 0
    provider_session_id = None
    raw_tail: list[str] = []
    try:
        while True:
            line = await _read_process_line(process)
            if line in (b"", "", None):
                break
            if isinstance(line, bytes):
                raw = line.decode("utf-8", errors="replace").strip()
            else:
                raw = str(line).strip()
            if raw:
                raw_tail.append(raw)
                raw_tail = raw_tail[-20:]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                if on_event and raw:
                    await _emit(on_event, {"event": "codex_output", "message": raw})
                continue

            provider_session_id = provider_session_id or _extract_provider_session_id(payload)
            event_type = str(payload.get("type") or payload.get("method") or "codex_event")
            if on_event:
                event: dict[str, Any] = {"event": event_type, "message": payload}
                if provider_session_id:
                    event["provider_session_id"] = provider_session_id
                await _emit(on_event, event)

            usage = _extract_usage(payload)
            if usage:
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
            message = _extract_agent_message(payload)
            if message:
                final_message = message
            parsed_error = _extract_error(payload)
            if parsed_error:
                error_message = parsed_error

        return_code = await _wait_scoped_process(process)
    except asyncio.CancelledError:
        await asyncio.to_thread(_terminate_scoped_process, process)
        raise
    except Exception as exc:
        await asyncio.to_thread(_terminate_scoped_process, process)
        error = f"scoped harness execution failed: {exc}"
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "codex_exec_failed",
                    "message": {"exit_code": None, "error": error},
                },
            )
        return RunResult(success=False, message=error)

    if return_code == 0:
        return RunResult(
            success=True,
            message=final_message or "\n".join(raw_tail),
            provider_session_id=provider_session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    message = error_message or "\n".join(raw_tail) or f"codex exec failed: {return_code}"
    if on_event:
        await _emit(
            on_event,
            {
                "event": "codex_exec_failed",
                "message": {"exit_code": return_code, "error": message},
                **(
                    {"provider_session_id": provider_session_id}
                    if provider_session_id
                    else {}
                ),
            },
        )
    return RunResult(
        success=False,
        message=message,
        provider_session_id=provider_session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


async def _run_plain_process(
    cmd: list[str],
    *,
    cwd: Path,
    event_name: str,
    failure_event: str,
    on_event: HarnessEventCallback | None = None,
) -> RunResult:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"}),
    )
    assert process.stdout is not None
    output_lines: list[str] = []
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue
        output_lines.append(text)
        output_lines = output_lines[-200:]
        if on_event:
            await _emit(on_event, {"event": event_name, "message": text})

    return_code = await process.wait()
    message = "\n".join(output_lines).strip()
    if return_code == 0:
        return RunResult(success=True, message=message)
    if on_event:
        await _emit(
            on_event,
            {
                "event": failure_event,
                "message": {"exit_code": return_code, "error": message},
            },
        )
    return RunResult(success=False, message=message or f"runner failed: {return_code}")


def _extract_agent_message(payload: dict[str, Any]) -> str:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    if item.get("type") == "agent_message":
        text = item.get("text")
        return text.strip() if isinstance(text, str) else ""
    message = payload.get("message")
    if isinstance(message, dict):
        text = message.get("content") or message.get("text")
        return text.strip() if isinstance(text, str) else ""
    return ""


def _extract_error(payload: dict[str, Any]) -> str:
    if payload.get("type") == "error" and isinstance(payload.get("message"), str):
        return payload["message"].strip()
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return message.strip() if isinstance(message, str) else ""
    return ""


def _extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
        "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
        "total_tokens": max(0, int(usage.get("total_tokens") or 0)),
    }


def _extract_provider_session_id(payload: dict[str, Any]) -> str | None:
    """Extract a provider-native continuation identifier from one event.

    The Codex/CLI providers have emitted ``session_id``, ``thread_id`` and
    ``conversation_id`` at different points in their JSONL protocols.  Keep
    accepting each raw spelling, but expose only ``provider_session_id`` to
    the harness and its API.  ``provider_session_id`` is accepted first for
    already-normalized custom/fake providers.
    """

    for key in (
        "provider_session_id",
        "session_id",
        "thread_id",
        "conversation_id",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # A few provider wrappers nest their envelope under ``thread`` or
    # ``session``.  Supporting those shapes is harmless and keeps the raw-ID
    # compatibility promise without making callers depend on a specific CLI
    # version.
    for container_key in ("thread", "session", "conversation"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in ("provider_session_id", "session_id", "thread_id", "conversation_id", "id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


# Backwards-compatible private helper name for integrations that imported it
# before provider-native IDs were given an explicit namespace.
def _extract_session_id(payload: dict[str, Any]) -> str | None:
    return _extract_provider_session_id(payload)
