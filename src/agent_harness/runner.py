"""Agent runner abstractions for the AoiTalk harness."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from .config import AgentHarnessSettings
from .models import HarnessEventCallback, RunResult, WorkItem

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
        cmd = self._command(workspace, prompt)
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

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "NO_COLOR": "1"},
        )

        assert process.stdout is not None
        final_message = ""
        error_message = ""
        input_tokens = output_tokens = total_tokens = 0
        session_id = None
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

            event_type = str(payload.get("type") or payload.get("method") or "codex_event")
            if on_event:
                await _emit(on_event, {"event": event_type, "message": payload})

            session_id = session_id or _extract_session_id(payload)
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
                session_id=session_id,
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
                },
            )
        return RunResult(
            success=False,
            message=message,
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def _command(self, workspace: Path, prompt: str) -> list[str]:
        bin_path = shutil.which(self.settings.codex.bin_path) or self.settings.codex.bin_path
        cmd = [
            bin_path,
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(workspace),
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


def build_runner(settings: AgentHarnessSettings) -> AgentRunner:
    runner_name = settings.codex.runner.strip().lower()
    if runner_name == "codex_exec":
        return CodexExecRunner(settings)
    raise ValueError(f"Unsupported agent harness runner: {settings.codex.runner}")


async def _emit(on_event: HarnessEventCallback, event: dict[str, Any]) -> None:
    maybe = on_event(event)
    if asyncio.iscoroutine(maybe):
        await maybe


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


def _extract_session_id(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "thread_id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
