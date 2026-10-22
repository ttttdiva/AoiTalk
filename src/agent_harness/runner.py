"""Agent runner abstractions for the AoiTalk harness."""

from __future__ import annotations

import asyncio
import contextlib
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
from ..utils.subprocess_env import (
    build_aoitalk_subprocess_env,
    windows_batch_command,
)

logger = logging.getLogger(__name__)


# Values intentionally forwarded into the guest namespace.  Host-derived
# paths, credentials, provider endpoints and arbitrary environment variables
# never cross the WSL/bwrap boundary.  Dynamic local preview uses only the
# loopback bind values below.
SCOPED_GUEST_PATH = "/opt/aoitalk/toolchain/bin:/usr/bin:/bin"

SCOPED_GUEST_ENV_ALLOWLIST = frozenset(
    {
        "BROWSER",
        "CI",
        "HOST",
        "HOSTNAME",
        "LANG",
        "LC_ALL",
        "NODE_ENV",
        "NO_COLOR",
        "PORT",
        "PATH",
        "PYTHON_DOTENV_DISABLED",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "TZ",
    }
)


def scoped_guest_env(values: dict[str, Any] | None = None) -> dict[str, str]:
    """Project safe scalar values into the guest process environment."""

    result: dict[str, str] = {}
    for name in SCOPED_GUEST_ENV_ALLOWLIST:
        value = (values or {}).get(name)
        if value is None:
            continue
        text = str(value)
        if not text or "\x00" in text or "\r" in text or "\n" in text:
            continue
        result[name] = SCOPED_GUEST_PATH if name == "PATH" else text[:512]
    return result


async def _create_local_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_file: Path | None = None,
) -> asyncio.subprocess.Process:
    """Start an unscoped runner, including npm-installed Windows wrappers."""

    executable = shutil.which(cmd[0]) or cmd[0]
    input_handle = input_file.open("rb") if input_file is not None else None
    stdin = input_handle or asyncio.subprocess.DEVNULL
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        wrapper_command = windows_batch_command([executable, *cmd[1:]])
        try:
            process = await asyncio.create_subprocess_shell(
                wrapper_command,
                cwd=str(cwd),
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        finally:
            if input_handle is not None:
                input_handle.close()
    else:
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *cmd[1:],
                cwd=str(cwd),
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        finally:
            if input_handle is not None:
                input_handle.close()
    return process


def _terminate_local_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a local runner process and its Windows child tree."""

    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            process.kill()
    except Exception:
        with contextlib.suppress(Exception):
            process.kill()


async def _run_codex_local_process(
    process: asyncio.subprocess.Process,
    *,
    on_event: HarnessEventCallback | None,
) -> RunResult:
    """Consume Codex JSONL and clean up the CLI when the caller is cancelled."""

    assert process.stdout is not None
    final_message = ""
    error_message = ""
    input_tokens = output_tokens = total_tokens = 0
    provider_session_id = None
    raw_tail: list[str] = []
    try:
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
    except asyncio.CancelledError:
        _terminate_local_process(process)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10)
        raise

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
                timeout_seconds=max(
                    float(self.settings.codex.stall_timeout_ms) / 1000.0,
                    1.0,
                ),
                on_event=on_event,
            )

        prompt_via_stdin = (
            os.name == "nt"
            and Path(shutil.which(cmd[0]) or cmd[0]).suffix.lower() in {".cmd", ".bat"}
        )
        prompt_file: Path | None = None
        try:
            if prompt_via_stdin:
                prompt_file = workspace / ".aoitalk" / ".agent-prompt.txt"
                prompt_file.parent.mkdir(parents=True, exist_ok=True)
                prompt_file.write_text(prompt, encoding="utf-8")
            process = await _create_local_process(
                [*cmd[:-1], "-"] if prompt_via_stdin else cmd,
                cwd=workspace,
                input_file=prompt_file,
                env=build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"}),
            )
            return await _run_codex_local_process(process, on_event=on_event)
        finally:
            if prompt_file is not None:
                with contextlib.suppress(OSError):
                    prompt_file.unlink()

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
        if active_scope is not None:
            from ..security.wsl_bwrap_backend import scoped_codex_executable

            configured_bin = scoped_codex_executable(configured_bin)
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
                timeout_seconds=max(
                    float(self.settings.codex.stall_timeout_ms) / 1000.0,
                    1.0,
                ),
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
        cmd = [bin_path, "-p", "--dangerously-skip-permissions"]
        if self.settings.claude.model:
            cmd.extend(["--model", self.settings.claude.model])
        if self.settings.claude.reasoning_effort:
            cmd.extend(["--effort", self.settings.claude.reasoning_effort])
        # Legacy harness permission_mode is retained for settings
        # compatibility, but no competing CLI flag is passed.
        if self.settings.claude.allowed_tools:
            # ``--allowedTools`` is variadic; ``--`` keeps the prompt positional.
            cmd.extend(["--allowedTools", *self.settings.claude.allowed_tools, "--"])
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
                timeout_seconds=max(
                    float(self.settings.codex.stall_timeout_ms) / 1000.0,
                    1.0,
                ),
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
    """Validate the server-selected scoped process backend before spawning.

    ``AgentRunScope`` is a context authority, not a hint.  Once present, a
    runner must use its selected backend or deny execution; it must never
    fall back from WSL2/bubblewrap to a host process.
    """

    try:
        from ..security.agent_run_scope import AgentRunScope
        from ..security.harness_execution_scope import resolve_command_backend_kind
    except Exception as exc:  # pragma: no cover - defensive stripped builds
        return f"scoped harness execution denied: sandbox backend unavailable ({exc})"

    if not isinstance(scope, AgentRunScope):
        return "scoped harness execution denied: invalid AgentRunScope"

    try:
        safe_cwd = scope.assert_command_cwd_allowed(cwd)
        backend_kind = resolve_command_backend_kind(run_scope=scope)
        if backend_kind == "native_windows":
            return "native_windows", Path(safe_cwd)
        if backend_kind != "wsl_bwrap":
            raise RuntimeError("scoped command backend is unavailable")
        from ..security.wsl_bwrap_backend import (
            WslBwrapError,
            get_wsl_bwrap_backend,
        )

        backend = get_wsl_bwrap_backend()
        if not bool(getattr(backend, "file_scoped", False)):
            raise WslBwrapError("configured backend is not file-scoped")
        is_available = getattr(backend, "is_available", None)
        if not callable(is_available) or not bool(is_available()):
            raise WslBwrapError(
                "file-scoped WSL2/bubblewrap backend is unavailable"
            )
        is_runtime_ready = getattr(backend, "is_runtime_ready", None)
        if not callable(is_runtime_ready) or not bool(is_runtime_ready()):
            raise WslBwrapError(
                "file-scoped WSL2/bubblewrap runtime smoke is unavailable"
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
        if getattr(scope, "backend_kind", "wsl_bwrap") == "native_windows":
            return str(safe_cwd)
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
    timeout_seconds: float,
) -> tuple[Any | None, str | None]:
    """Spawn one child through the server-selected run-scoped backend."""

    prepared = _scoped_backend(scope, cwd)
    if isinstance(prepared, str):
        return None, prepared
    backend, safe_cwd = prepared
    env = build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"})
    try:
        if backend == "native_windows":
            if os.name != "nt":
                return None, "scoped harness execution denied: native_windows requires Windows"
            from ..tools.os_operations.command_executor import build_process_group_kwargs

            process_group_kwargs = build_process_group_kwargs()
            process_group_kwargs["creationflags"] |= getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
            process = subprocess.Popen(
                [str(part) for part in cmd],
                cwd=str(safe_cwd),
                env=env,
                **process_group_kwargs,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
            process._aoitalk_backend_kind = "native_windows"
        else:
            command = shlex.join(str(part) for part in cmd)
            guest_env = scoped_guest_env({"NO_COLOR": "1", "CI": "1", "PATH": SCOPED_GUEST_PATH})
            process = backend.spawn(
                scope,
                command,
                cwd=safe_cwd,
                shell="bash",
                env=env,
                guest_env=guest_env,
                timeout=max(float(timeout_seconds), 1.0),
                popen_kwargs={
                    **build_process_group_kwargs(),
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
    if backend != "native_windows" and getattr(process, "_aoitalk_finalization_state", None) is None:
        _terminate_scoped_process(process)
        return None, "scoped harness execution denied: backend returned no finalization state"
    return process, None


def _scoped_output_limit() -> int:
    try:
        from ..security.harness_execution_scope import (
            get_current_harness_execution_scope,
        )

        upper = get_current_harness_execution_scope()
        limits = getattr(upper, "resource_limits", None) if upper is not None else None
        if limits is not None:
            return max(1, min(int(limits.max_output_bytes), 64 * 1024 * 1024))
    except Exception:
        pass
    return 32_768


async def _read_process_line(
    process: Any,
    *,
    limit: int | None = None,
) -> bytes | str:
    """Read one bounded line without blocking the asyncio event loop."""

    stream = getattr(process, "stdout", None)
    if stream is None:
        return b""
    # WslBwrapBackend owns a synchronous ``subprocess.Popen``.  Test doubles
    # and legacy adapters may expose an async readline; support both without
    # changing the host subprocess path.
    limit = _scoped_output_limit() if limit is None else max(int(limit), 1)
    try:
        value = await asyncio.to_thread(stream.readline, limit + 1)
    except TypeError:  # pragma: no cover - minimal async/test stream adapter
        value = await asyncio.to_thread(stream.readline)
    if inspect.isawaitable(value):
        value = await value
    if isinstance(value, (bytes, str)) and len(value) > limit:
        newline = b"\n" if isinstance(value, bytes) else "\n"
        # Drain the remainder of an oversized logical line in bounded chunks;
        # retaining it would let one no-newline runner message exhaust host
        # memory before the output/event tail cap can run.
        while value and not value.endswith(newline):
            try:
                remainder = await asyncio.to_thread(stream.readline, limit + 1)
            except TypeError:  # pragma: no cover
                remainder = await asyncio.to_thread(stream.readline)
            if inspect.isawaitable(remainder):
                remainder = await remainder
            if not remainder or remainder.endswith(newline):
                break
        marker = (
            b"...[scoped runner line truncated]"
            if isinstance(value, bytes)
            else "...[scoped runner line truncated]"
        )
        value = value[: max(limit - len(marker), 0)] + marker
    return value


def _bounded_output_tail(current: str, value: str, *, limit: int = 32_768) -> str:
    combined = f"{current}\n{value}" if current else value
    payload = combined.encode("utf-8", errors="replace")
    if len(payload) <= limit:
        return combined
    marker = b"...[scoped runner output truncated]...\n"
    tail_size = max(limit - len(marker), 0)
    tail = payload[-tail_size:] if tail_size else b""
    return (marker[:limit] + tail).decode("utf-8", errors="replace")


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


async def _await_scoped_finalization(
    process: Any,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Wait for the backend's exactly-once staged publication result."""

    state = getattr(process, "_aoitalk_finalization_state", None)
    if state is None:
        return {"success": False, "error": "scoped backend returned no finalization state"}
    try:
        budget = float(getattr(state, "finalization_timeout_seconds", 10.0))
        if timeout_seconds is not None:
            budget = min(budget, max(float(timeout_seconds), 0.1))
        outcome = await asyncio.wait_for(
            asyncio.to_thread(state.wait, budget),
            timeout=budget + 1.0,
        )
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            state.cancel("staged publication finalization cancelled")
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            state.cancel("staged publication finalization timeout")
        return {"success": False, "error": f"staged finalization failed: {type(exc).__name__}"}
    if outcome is None:
        with contextlib.suppress(Exception):
            state.cancel("staged publication finalization timeout")
        return {"success": False, "error": "staged publication finalization timeout"}
    if not isinstance(outcome, dict):
        return {"success": False, "error": "staged publication finalization returned invalid result"}
    return dict(outcome)


async def _cancel_scoped_process(process: Any, reason: str) -> dict[str, Any]:
    """Cancel state before killing a scoped process, then drain finalization."""

    state = getattr(process, "_aoitalk_finalization_state", None)
    if state is not None:
        with contextlib.suppress(Exception):
            state.cancel(str(reason))
    try:
        await asyncio.to_thread(_terminate_scoped_process, process)
    except asyncio.CancelledError:
        return {"success": False, "error": "scoped process termination was interrupted"}
    except Exception:
        # Continue to the bounded wait/finalization checks so the caller gets
        # an explicit uncertain cleanup result rather than a host fallback.
        pass
    wait_task = asyncio.create_task(_wait_scoped_process(process))
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=10.0)
    except asyncio.CancelledError:
        wait_task.cancel()
        return {"success": False, "error": "scoped process cancellation cleanup was interrupted"}
    except asyncio.TimeoutError:
        wait_task.cancel()
        return {"success": False, "error": "scoped process did not terminate within 10 seconds"}
    except Exception as exc:
        return {"success": False, "error": f"scoped process wait failed: {type(exc).__name__}"}
    if getattr(process, "_aoitalk_backend_kind", None) == "native_windows":
        return {"success": True, "backend_kind": "native_windows", "filesystem_isolation": False}
    finalize_task = asyncio.create_task(_await_scoped_finalization(process))
    try:
        return await asyncio.shield(finalize_task)
    except asyncio.CancelledError:
        finalize_task.cancel()
        return {"success": False, "error": "staged finalization cleanup was interrupted"}
    except asyncio.TimeoutError:
        finalize_task.cancel()
        return {"success": False, "error": "staged publication finalization did not complete"}
    except Exception as exc:
        return {"success": False, "error": f"staged finalization failed: {type(exc).__name__}"}


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
    timeout_seconds: float = 300.0,
    on_event: HarnessEventCallback | None = None,
) -> RunResult:
    """Run Claude/custom harness output through WSL2+bwrap."""

    process, error = await _spawn_scoped_process(
        cmd,
        scope=scope,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
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

    output_tail = ""
    output_limit = _scoped_output_limit()
    deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.1)
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(
                _read_process_line(process, limit=output_limit),
                timeout=remaining,
            )
            if line in (b"", "", None):
                break
            if isinstance(line, bytes):
                text = line.decode("utf-8", errors="replace").rstrip()
            else:
                text = str(line).rstrip()
            if not text:
                continue
            output_tail = _bounded_output_tail(
                output_tail,
                text,
                limit=output_limit,
            )
            if on_event:
                await _emit(on_event, {"event": event_name, "message": text})
        return_code = await _wait_scoped_process(process)
    except asyncio.CancelledError:
        await _cancel_scoped_process(process, "scoped runner cancelled")
        raise
    except asyncio.TimeoutError:
        await _cancel_scoped_process(process, "scoped runner timed out")
        error = f"scoped harness execution timed out after {timeout_seconds} seconds"
        if on_event:
            await _emit(on_event, {"event": failure_event, "message": {"exit_code": None, "error": error}})
        return RunResult(success=False, message=error)
    except Exception as exc:
        await _cancel_scoped_process(process, f"scoped runner failed: {type(exc).__name__}")
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

    finalization = (
        {"success": True, "backend_kind": "native_windows", "filesystem_isolation": False}
        if getattr(process, "_aoitalk_backend_kind", None) == "native_windows"
        else await _await_scoped_finalization(process)
    )
    if not finalization.get("success", False) or finalization.get("discarded", False):
        error = str(finalization.get("error") or finalization.get("reason") or "staged publication finalization failed")
        if on_event:
            await _emit(
                on_event,
                {"event": failure_event, "message": {"exit_code": return_code, "error": error}},
            )
        return RunResult(success=False, message=error)
    message = output_tail.strip()
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
    timeout_seconds: float = 300.0,
    on_event: HarnessEventCallback | None = None,
) -> RunResult:
    """Run Codex JSONL output through WSL2+bwrap without host subprocesses."""

    process, error = await _spawn_scoped_process(
        cmd,
        scope=scope,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
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
    raw_tail = ""
    output_limit = _scoped_output_limit()
    deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.1)
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(
                _read_process_line(process, limit=output_limit),
                timeout=remaining,
            )
            if line in (b"", "", None):
                break
            if isinstance(line, bytes):
                raw = line.decode("utf-8", errors="replace").strip()
            else:
                raw = str(line).strip()
            if raw:
                raw_tail = _bounded_output_tail(
                    raw_tail,
                    raw,
                    limit=output_limit,
                )
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
                final_message = _bounded_output_tail(
                    "",
                    message,
                    limit=output_limit,
                )
            parsed_error = _extract_error(payload)
            if parsed_error:
                error_message = _bounded_output_tail(
                    "",
                    parsed_error,
                    limit=output_limit,
                )

        return_code = await _wait_scoped_process(process)
    except asyncio.CancelledError:
        await _cancel_scoped_process(process, "scoped Codex runner cancelled")
        raise
    except asyncio.TimeoutError:
        await _cancel_scoped_process(process, "scoped Codex runner timed out")
        error = f"scoped harness execution timed out after {timeout_seconds} seconds"
        if on_event:
            await _emit(on_event, {"event": "codex_exec_failed", "message": {"exit_code": None, "error": error}})
        return RunResult(success=False, message=error)
    except Exception as exc:
        await _cancel_scoped_process(process, f"scoped Codex runner failed: {type(exc).__name__}")
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

    finalization = (
        {"success": True, "backend_kind": "native_windows", "filesystem_isolation": False}
        if getattr(process, "_aoitalk_backend_kind", None) == "native_windows"
        else await _await_scoped_finalization(process)
    )
    if not finalization.get("success", False) or finalization.get("discarded", False):
        message = str(finalization.get("error") or finalization.get("reason") or "staged publication finalization failed")
        if on_event:
            await _emit(
                on_event,
                {
                    "event": "codex_exec_failed",
                    "message": {"exit_code": return_code, "error": message},
                },
            )
        return RunResult(success=False, message=message, provider_session_id=provider_session_id)
    if return_code == 0:
        return RunResult(
            success=True,
            message=final_message or raw_tail,
            provider_session_id=provider_session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    message = error_message or raw_tail or f"codex exec failed: {return_code}"
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
    process = await _create_local_process(
        cmd,
        cwd=cwd,
        env=build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"}),
    )
    assert process.stdout is not None
    output_lines: list[str] = []
    try:
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
    except asyncio.CancelledError:
        _terminate_local_process(process)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10)
        raise
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
