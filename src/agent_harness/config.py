"""Configuration parsing for the AoiTalk agent harness."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        return list(default)
    return [item for item in items if item]


def _resolve_path(value: str | None, *, root_dir: Path, default: str) -> Path:
    raw = value or default
    if raw.startswith("$") and raw[1:].replace("_", "A").isalnum():
        raw = os.environ.get(raw[1:], default)
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        path = root_dir / path
    return path


@dataclass(frozen=True)
class HarnessTrackerSettings:
    active_states: list[str] = field(
        default_factory=lambda: ["todo", "open", "in_progress", "review"]
    )
    terminal_states: list[str] = field(default_factory=lambda: ["closed", "cancelled"])
    include_all_active_tasks: bool = False
    project_id: str | None = None


@dataclass(frozen=True)
class HarnessHookSettings:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass(frozen=True)
class HarnessCodexSettings:
    bin_path: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    approval_policy: Any = "never"
    exec_sandbox: str = "workspace-write"
    stall_timeout_ms: int = 300_000
    runner: str = "codex_exec"


@dataclass(frozen=True)
class HarnessClaudeSettings:
    bin_path: str = "claude"
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class HarnessCustomCommandSettings:
    command: str | None = None
    args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentHarnessSettings:
    enabled: bool = False
    auto_start: bool = False
    polling_interval_ms: int = 30_000
    workflow_file: Path = Path("config/agent_harness/WORKFLOW.md")
    workspace_root: Path = Path("cache/agent_workspaces")
    workspace_base_ref: str = "origin/main"
    workspace_branch_prefix: str = "harness/"
    max_concurrent_agents: int = 1
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    failure_retry_base_ms: int = 10_000
    tracker: HarnessTrackerSettings = field(default_factory=HarnessTrackerSettings)
    hooks: HarnessHookSettings = field(default_factory=HarnessHookSettings)
    codex: HarnessCodexSettings = field(default_factory=HarnessCodexSettings)
    claude: HarnessClaudeSettings = field(default_factory=HarnessClaudeSettings)
    custom_command: HarnessCustomCommandSettings = field(default_factory=HarnessCustomCommandSettings)

    @classmethod
    def from_config(
        cls, config: Any, *, root_dir: Path | str | None = None
    ) -> "AgentHarnessSettings":
        root = Path(root_dir) if root_dir is not None else Path.cwd()
        raw = _config_get(config, "agent_harness", {}) or {}

        tracker_raw = raw.get("tracker", {}) or {}
        hooks_raw = raw.get("hooks", {}) or {}
        codex_raw = raw.get("codex", {}) or {}
        claude_raw = raw.get("claude", {}) or {}
        custom_command_raw = raw.get("custom_command", {}) or {}
        codex_cli_raw = _config_get(config, "codex_cli", {}) or {}
        claude_cli_raw = _config_get(config, "claude_cli", {}) or {}
        try:
            from ..services.agent_team_service import (
                agent_team_member_for,
                model_route_is_explicit,
                resolve_agent_team_member_mode,
            )

            agent_route = (
                agent_team_member_for(config, "agent_harness")
                if model_route_is_explicit(config, "agent_harness")
                else None
            )
        except Exception:
            agent_route = None
        route_codex_model = None
        route_codex_effort = None
        route_claude_model = None
        route_claude_effort = None
        route_runner = None
        # Bare legacy configs without agent_team retain their old behavior;
        # application configs always contain the global/member switches.
        agent_team_harness_enabled = _config_get(config, "agent_team", None) is None
        if agent_route:
            route_runner = agent_route.get("runner")
            agent_team_harness_enabled = True
        normalized_route_runner = _normalize_runner(str(route_runner)) if route_runner else ""
        if agent_route and (
            normalized_route_runner in {"codex_exec", "codex_cli"}
            or (
                not normalized_route_runner
                and agent_route.get("provider") == "codex-cli"
            )
        ):
            route_codex_model = agent_route.get("model")
            route_codex_effort = resolve_agent_team_member_mode(config, member_key="agent_harness", provider="codex-cli", model=str(route_codex_model or ""))
            route_runner = route_runner or "codex_exec"
        elif agent_route and (
            normalized_route_runner in {"claude_code", "claude_cli"}
            or agent_route.get("provider") == "claude-cli"
        ):
            route_claude_model = agent_route.get("model")
            route_claude_effort = resolve_agent_team_member_mode(config, member_key="agent_harness", provider="claude-cli", model=str(route_claude_model or ""))
            if not route_runner or _normalize_runner(str(route_runner)) == "codex_exec":
                route_runner = "claude_code"
        by_state_raw = raw.get("max_concurrent_agents_by_state", {}) or {}
        by_state = {
            _normalize_state(str(state)): int(limit)
            for state, limit in by_state_raw.items()
            if _is_positive_int(limit)
        }

        return cls(
            enabled=_as_bool(raw.get("enabled"), False) and agent_team_harness_enabled,
            auto_start=_as_bool(raw.get("auto_start"), False),
            polling_interval_ms=_as_int(raw.get("polling_interval_ms"), 30_000, minimum=1),
            workflow_file=_resolve_path(
                raw.get("workflow_file"),
                root_dir=root,
                default="config/agent_harness/WORKFLOW.md",
            ),
            workspace_root=_resolve_path(
                raw.get("workspace_root"),
                root_dir=root,
                default="cache/agent_workspaces",
            ),
            workspace_base_ref=str(raw.get("workspace_base_ref") or "origin/main"),
            workspace_branch_prefix=str(raw.get("workspace_branch_prefix") or "harness/"),
            max_concurrent_agents=_as_int(raw.get("max_concurrent_agents"), 1, minimum=1),
            max_concurrent_agents_by_state=by_state,
            max_turns=_as_int(raw.get("max_turns"), 20, minimum=1),
            max_retry_backoff_ms=_as_int(raw.get("max_retry_backoff_ms"), 300_000, minimum=1),
            failure_retry_base_ms=_as_int(raw.get("failure_retry_base_ms"), 10_000, minimum=1),
            tracker=HarnessTrackerSettings(
                active_states=_as_str_list(
                    tracker_raw.get("active_states"),
                    ["todo", "open", "in_progress", "review"],
                ),
                terminal_states=_as_str_list(
                    tracker_raw.get("terminal_states"), ["closed", "cancelled"]
                ),
                include_all_active_tasks=_as_bool(
                    tracker_raw.get("include_all_active_tasks"), False
                ),
                project_id=tracker_raw.get("project_id"),
            ),
            hooks=HarnessHookSettings(
                after_create=hooks_raw.get("after_create"),
                before_run=hooks_raw.get("before_run"),
                after_run=hooks_raw.get("after_run"),
                before_remove=hooks_raw.get("before_remove"),
                timeout_ms=_as_int(hooks_raw.get("timeout_ms"), 60_000, minimum=1),
            ),
            codex=HarnessCodexSettings(
                bin_path=str(codex_raw.get("bin_path") or os.getenv("CODEX_BIN") or "codex"),
                model=route_codex_model or codex_raw.get("model") or codex_cli_raw.get("model"),
                reasoning_effort=(
                    route_codex_effort
                    or codex_raw.get("reasoning_effort")
                    or codex_cli_raw.get("reasoning_effort")
                ),
                approval_policy=codex_raw.get("approval_policy", "never"),
                exec_sandbox=str(codex_raw.get("exec_sandbox") or "workspace-write"),
                stall_timeout_ms=_as_int(codex_raw.get("stall_timeout_ms"), 300_000, minimum=0),
                runner=_normalize_runner(str(route_runner or codex_raw.get("runner") or "codex_exec")),
            ),
            claude=HarnessClaudeSettings(
                bin_path=str(claude_raw.get("bin_path") or os.getenv("CLAUDE_BIN") or "claude"),
                model=route_claude_model or claude_raw.get("model") or claude_cli_raw.get("model"),
                reasoning_effort=(
                    route_claude_effort
                    or claude_raw.get("reasoning_effort")
                    or claude_cli_raw.get("reasoning_effort")
                ),
            ),
            custom_command=HarnessCustomCommandSettings(
                command=custom_command_raw.get("command"),
                args=_as_str_list(custom_command_raw.get("args"), []),
            ),
        )


def _is_positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _normalize_state(state: str) -> str:
    return state.strip().lower()


def _normalize_runner(runner: str) -> str:
    return runner.strip().lower().replace("-", "_") or "codex_exec"
