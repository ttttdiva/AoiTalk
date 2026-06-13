from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_harness.config import AgentHarnessSettings, HarnessHookSettings
from src.agent_harness.models import RunResult, WorkItem
from src.agent_harness.orchestrator import AgentHarnessOrchestrator, sort_work_items_for_dispatch
from src.agent_harness.runner import AgentRunner, CodexExecRunner, build_runner
from src.agent_harness.tracker import InMemoryWorkItemTracker, _task_harness_enabled
from src.agent_harness.workflow import HarnessWorkflow, WorkflowRenderError, load_harness_workflow, render_prompt
from src.agent_harness.workspace import (
    WorkspaceManager,
    WorkspaceSafetyError,
    assert_path_within_root,
    run_hook,
    sanitize_identifier,
)


class ImmediateRunner(AgentRunner):
    def __init__(self, *, success: bool = True, message: str = "ok"):
        self.success = success
        self.message = message
        self.prompts: list[str] = []

    async def run(self, *, work_item, workspace, prompt, attempt, on_event=None):
        self.prompts.append(prompt)
        if on_event:
            maybe = on_event(
                {
                    "event": "completed",
                    "session_id": f"session-{attempt}",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "total_tokens": 3,
                    },
                }
            )
            if asyncio.iscoroutine(maybe):
                await maybe
        return RunResult(
            success=self.success,
            message=self.message,
            input_tokens=4,
            output_tokens=5,
            total_tokens=9,
        )


class BlockingRunner(AgentRunner):
    def __init__(self):
        self.release = asyncio.Event()

    async def run(self, *, work_item, workspace, prompt, attempt, on_event=None):
        await self.release.wait()
        return RunResult(success=True, message="released")


def item(
    item_id: str,
    *,
    identifier: str | None = None,
    state: str = "todo",
    priority: str = "medium",
    created_at: datetime | None = None,
    blocked_by: list[dict] | None = None,
) -> WorkItem:
    return WorkItem(
        id=item_id,
        identifier=identifier or item_id,
        title=f"Task {item_id}",
        description="Do the thing",
        state=state,
        priority=priority,
        created_at=created_at or datetime(2026, 1, 1),
        blocked_by=blocked_by or [],
    )


def settings(tmp_path: Path, *, enabled: bool = True) -> AgentHarnessSettings:
    return AgentHarnessSettings.from_config(
        {
            "agent_harness": {
                "enabled": enabled,
                "workspace_root": str(tmp_path / "workspaces"),
                "workflow_file": str(tmp_path / "WORKFLOW.md"),
                "max_concurrent_agents": 1,
                "failure_retry_base_ms": 10,
                "max_retry_backoff_ms": 50,
                "codex": {"runner": "codex_exec", "stall_timeout_ms": 1},
            }
        },
        root_dir=tmp_path,
    )


def orchestrator(tmp_path: Path, tracker, runner, *, enabled: bool = True):
    cfg = settings(tmp_path, enabled=enabled)
    return AgentHarnessOrchestrator(
        settings=cfg,
        tracker=tracker,
        runner=runner,
        workspace_manager=WorkspaceManager(cfg.workspace_root, cfg.hooks),
        workflow=HarnessWorkflow(
            path=tmp_path / "WORKFLOW.md",
            prompt_template="Run {{ issue.identifier }} attempt {{ attempt }}",
        ),
    )


def test_workflow_front_matter_parse_and_strict_render(tmp_path):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        "---\nname: harness\ncustom_flag: true\n---\nHello {{ issue.title }} #{{ attempt }}\n",
        encoding="utf-8",
    )

    workflow = load_harness_workflow(workflow_path)
    rendered = render_prompt(workflow, issue=item("1", identifier="TASK-1"), attempt=2)

    assert workflow.metadata["custom_flag"] is True
    assert rendered == "Hello Task 1 #2"
    with pytest.raises(WorkflowRenderError):
        render_prompt(
            HarnessWorkflow(path=workflow_path, prompt_template="{{ issue.missing }}"),
            issue=item("1"),
        )


def test_env_and_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("AOITALK_HARNESS_ROOT", str(tmp_path / "env-workspaces"))
    cfg = AgentHarnessSettings.from_config(
        {
            "agent_harness": {
                "workspace_root": "$AOITALK_HARNESS_ROOT",
                "workflow_file": "config/agent_harness/WORKFLOW.md",
                "max_concurrent_agents_by_state": {"todo": 2, "bad": 0},
            }
        },
        root_dir=tmp_path,
    )

    assert cfg.workspace_root == tmp_path / "env-workspaces"
    assert cfg.workflow_file == tmp_path / "config" / "agent_harness" / "WORKFLOW.md"
    assert cfg.max_concurrent_agents_by_state == {"todo": 2}


def test_legacy_dry_run_runner_is_not_silently_rewritten(tmp_path):
    cfg = AgentHarnessSettings.from_config(
        {
            "codex_cli": {"model": "gpt-5.5", "reasoning_effort": "high"},
            "agent_harness": {
                "workspace_root": str(tmp_path / "workspaces"),
                "codex": {"runner": "dry_run"},
            },
        },
        root_dir=tmp_path,
    )

    assert cfg.codex.runner == "dry_run"
    assert cfg.codex.model == "gpt-5.5"
    assert cfg.codex.reasoning_effort == "high"
    with pytest.raises(ValueError, match="Unsupported agent harness runner"):
        build_runner(cfg)


def test_codex_exec_runner_command_is_real_workspace_agent(tmp_path):
    cfg = AgentHarnessSettings.from_config(
        {
            "codex_cli": {"model": "gpt-5.5", "reasoning_effort": "high"},
            "agent_harness": {
                "workspace_root": str(tmp_path / "workspaces"),
                "codex": {"runner": "codex_exec"},
            },
        },
        root_dir=tmp_path,
    )

    cmd = CodexExecRunner(cfg)._command(tmp_path, "do work")

    assert cmd[:2][-1] == "exec"
    assert "--model" in cmd and "gpt-5.5" in cmd
    assert 'model_reasoning_effort="high"' in cmd
    assert "--sandbox" in cmd and "workspace-write" in cmd
    assert "--ignore-rules" not in cmd
    assert "--ephemeral" not in cmd
    assert "read-only" not in cmd


def test_workspace_identifier_sanitization_and_root_escape(tmp_path):
    root = tmp_path / "root"
    assert sanitize_identifier("../TASK 1:/x") == "TASK_1__x"
    assert_path_within_root(root / "TASK-1", root)
    with pytest.raises(WorkspaceSafetyError):
        assert_path_within_root(root / ".." / "outside", root)


def test_hook_success_failure_and_timeout(tmp_path):
    ok = run_hook("ok", f'"{sys.executable}" -c "print(123)"', tmp_path, timeout_ms=1000)
    fail = run_hook("fail", f'"{sys.executable}" -c "import sys; sys.exit(7)"', tmp_path, timeout_ms=1000)
    timeout = run_hook(
        "timeout",
        f'"{sys.executable}" -c "import time; time.sleep(0.2)"',
        tmp_path,
        timeout_ms=50,
    )

    assert ok.ok is True and "123" in ok.output
    assert fail.ok is False and fail.status == 7
    assert timeout.ok is False and timeout.timed_out is True


def test_workspace_manager_creates_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_hook("git-init", "git init -b main", repo, timeout_ms=1000)
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    run_hook("git-add", "git add README.md", repo, timeout_ms=1000)
    commit = run_hook(
        "git-commit",
        'git -c user.name=Test -c user.email=test@example.com commit -m init',
        repo,
        timeout_ms=1000,
    )
    assert commit.ok, commit.output

    manager = WorkspaceManager(
        tmp_path / "workspaces",
        HarnessHookSettings(),
        repo_root=repo,
        base_ref="main",
        branch_prefix="harness/",
    )
    workspace, created = manager.create_for("TASK 1")

    assert created is True
    assert (workspace / ".git").exists()
    branch = run_hook("branch", "git branch --show-current", workspace, timeout_ms=1000)
    assert branch.output.strip() == "harness/TASK_1"


@pytest.mark.asyncio
async def test_before_run_hook_failure_schedules_retry(tmp_path):
    cfg = settings(tmp_path)
    cfg = replace(
        cfg,
        hooks=replace(
            cfg.hooks,
            before_run=f'"{sys.executable}" -c "import sys; sys.exit(9)"',
        ),
    )
    tracker = InMemoryWorkItemTracker([item("1")])
    orch = AgentHarnessOrchestrator(
        settings=cfg,
        tracker=tracker,
        runner=ImmediateRunner(),
        workspace_manager=WorkspaceManager(cfg.workspace_root, cfg.hooks),
        workflow=HarnessWorkflow(path=tmp_path / "WORKFLOW.md", prompt_template="x"),
    )

    await orch.tick()

    assert not orch.running
    assert "1" in orch.retry_attempts
    assert "before_run hook failed" in (orch.retry_attempts["1"].error or "")


def test_task_harness_enabled_flag_selection():
    assert _task_harness_enabled(SimpleNamespace(task_metadata={"agent_harness": {"enabled": True}}))
    assert _task_harness_enabled(SimpleNamespace(task_metadata={"agent_harness_enabled": True}))
    assert not _task_harness_enabled(SimpleNamespace(task_metadata={}))


@pytest.mark.asyncio
async def test_priority_status_sorting_and_blocker_skip(tmp_path):
    old = datetime(2026, 1, 1)
    new = datetime(2026, 1, 2)
    items = [
        item("low", priority="low", created_at=old),
        item("urgent-new", priority="urgent", created_at=new),
        item("urgent-old", priority="urgent", created_at=old),
    ]

    assert [i.id for i in sort_work_items_for_dispatch(items)] == [
        "urgent-old",
        "urgent-new",
        "low",
    ]

    blocked = item("blocked", blocked_by=[{"id": "parent", "state": "open"}])
    tracker = InMemoryWorkItemTracker([blocked])
    orch = orchestrator(tmp_path, tracker, ImmediateRunner())
    await orch.tick()
    assert not orch.running
    assert "blocked" not in orch.claimed


@pytest.mark.asyncio
async def test_claimed_running_duplicate_dispatch_prevention(tmp_path):
    tracker = InMemoryWorkItemTracker([item("1")])
    runner = BlockingRunner()
    orch = orchestrator(tmp_path, tracker, runner)

    await orch.tick()
    await orch.tick()

    assert list(orch.running) == ["1"]
    assert orch.snapshot()["claimed"] == ["1"]
    runner.release.set()
    await asyncio.sleep(0)
    await orch.tick()
    await orch.tick()
    assert "1" in orch.completed
    assert "1" not in orch.retry_attempts
    assert "1" not in orch.running


@pytest.mark.asyncio
async def test_retry_backoff_and_retry_dispatch(tmp_path):
    tracker = InMemoryWorkItemTracker([item("1")])
    orch = orchestrator(tmp_path, tracker, ImmediateRunner(success=False, message="boom"))

    await orch.tick()
    await asyncio.sleep(0)
    await orch.tick()

    retry = orch.retry_attempts["1"]
    assert retry.attempt == 2
    assert retry.error == "boom"
    assert 0 <= (retry.due_at - datetime.utcnow()).total_seconds() <= 0.2

    retry.due_at = datetime.utcnow() - timedelta(seconds=1)
    await orch.tick()
    assert "1" in orch.running


@pytest.mark.asyncio
async def test_terminal_reconciliation_releases_claim_and_removes_workspace(tmp_path):
    active = item("1", identifier="TASK-1")
    tracker = InMemoryWorkItemTracker([active])
    runner = BlockingRunner()
    orch = orchestrator(tmp_path, tracker, runner)

    await orch.tick()
    workspace = orch.running["1"].workspace_path
    assert workspace.exists()
    tracker.set_item(replace(active, state="closed"))
    await orch.tick()

    assert "1" not in orch.running
    assert "1" not in orch.claimed
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_stall_timeout_stops_and_retries(tmp_path):
    tracker = InMemoryWorkItemTracker([item("1")])
    runner = BlockingRunner()
    orch = orchestrator(tmp_path, tracker, runner)

    await orch.tick()
    entry = orch.running["1"]
    entry.started_at = datetime.utcnow() - timedelta(seconds=1)
    await orch.tick()

    assert "1" not in orch.running
    assert "1" in orch.retry_attempts
    assert "stalled" in (orch.retry_attempts["1"].error or "")
