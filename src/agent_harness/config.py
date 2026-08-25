"""Configuration parsing for the AoiTalk agent harness."""

from __future__ import annotations

import os
import copy
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# The HTTP settings endpoint intentionally exposes a much smaller contract
# than the on-disk Agent Harness configuration.  In particular, path,
# executable, shell-hook, and custom-command settings are deployment-owned;
# accepting those values from a request would turn a configuration update into
# an arbitrary process/workspace execution primitive.
AGENT_HARNESS_UPDATE_KEYS = frozenset(
    {
        "enabled",
        "auto_start",
        "polling_interval_ms",
        "max_concurrent_agents",
        "max_concurrent_agents_by_state",
        "max_turns",
        "max_retry_backoff_ms",
        "failure_retry_base_ms",
        "tracker",
        "codex",
        "claude",
        # These aliases are emitted by the current settings UI.  They are
        # normalized into ``codex`` below and are not persisted as executable
        # top-level options.
        "runner",
        "model",
        "effort",
    }
)
AGENT_HARNESS_TRACKER_UPDATE_KEYS = frozenset(
    {"active_states", "terminal_states", "include_all_active_tasks", "project_id"}
)
AGENT_HARNESS_CODEX_UPDATE_KEYS = frozenset(
    {"model", "reasoning_effort", "approval_policy", "exec_sandbox", "runner", "stall_timeout_ms"}
)
AGENT_HARNESS_CLAUDE_UPDATE_KEYS = frozenset({"model", "reasoning_effort"})
AGENT_HARNESS_SAFE_RUNNERS = frozenset(
    {"codex_exec", "codex_cli", "claude_code", "claude_cli", "custom_command"}
)
AGENT_HARNESS_SAFE_SANDBOXES = frozenset({"read-only", "workspace-write"})
AGENT_HARNESS_SAFE_APPROVAL_POLICIES = frozenset(
    {"never", "on-request", "on-failure", "untrusted", "unless-trusted"}
)
_HARNESS_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "token", "credential", "credentials", "secret", "secrets", "base_url"}
)
_HARNESS_SHELL_META = re.compile(r"[\x00\r\n;&|<>`$]")


class AgentHarnessConfigError(ValueError):
    """Raised when an untrusted Agent Harness settings payload is invalid."""


def _reject_harness_secrets(value: Any, *, path: str = "settings") -> None:
    """Reject credential-shaped keys at every nesting level."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _HARNESS_SECRET_KEYS:
                raise AgentHarnessConfigError(
                    f"Provider credentials cannot be stored in Agent Harness settings: {path}.{key}"
                )
            _reject_harness_secrets(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_harness_secrets(child, path=f"{path}[{index}]")


def _validate_harness_text(value: Any, *, field_name: str, max_length: int = 512) -> str:
    if not isinstance(value, str):
        raise AgentHarnessConfigError(f"{field_name} must be a string")
    text = value.strip()
    if (
        not text
        or len(text) > max_length
        or any(ord(char) < 32 for char in text)
        or _HARNESS_SHELL_META.search(text)
        or any(char in text for char in "\"'")
    ):
        raise AgentHarnessConfigError(f"Invalid {field_name}")
    return text


def _validate_harness_string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise AgentHarnessConfigError(f"{field_name} must be an array of strings")
    result: list[str] = []
    for item in value:
        result.append(_validate_harness_text(item, field_name=field_name, max_length=128))
    return list(dict.fromkeys(result))


def _validate_harness_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    # bool is an int subclass, but accepting it here makes an accidental
    # ``enabled``/limit shape surprisingly permissive.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentHarnessConfigError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise AgentHarnessConfigError(f"Invalid {field_name}")
    return value


def validate_agent_harness_update(payload: Any) -> dict[str, Any]:
    """Validate and canonicalize the public Agent Harness settings contract.

    The function is shared by the API layer and tests.  It deliberately does
    not accept deployment-owned paths, binaries, shell hooks, or a custom
    command runner.  Existing YAML/environment configurations remain usable
    through :meth:`AgentHarnessSettings.from_config`; only untrusted request
    updates use this restricted schema.
    """

    if not isinstance(payload, dict):
        raise AgentHarnessConfigError("settings must be an object")
    _reject_harness_secrets(payload)

    if "settings" in payload:
        if set(payload) != {"settings"} or not isinstance(payload["settings"], dict):
            raise AgentHarnessConfigError("Only the settings object is accepted")
        raw = payload["settings"]
    else:
        raw = payload
    if not isinstance(raw, dict):
        raise AgentHarnessConfigError("settings must be an object")
    _reject_harness_secrets(raw)

    unknown = set(raw) - AGENT_HARNESS_UPDATE_KEYS
    if unknown:
        # Mention the key, not its value: values may contain sensitive paths or
        # command text and must never be reflected in an error response.
        raise AgentHarnessConfigError(f"Unsupported Agent Harness setting: {sorted(str(item) for item in unknown)[0]}")

    result: dict[str, Any] = {}
    for key in (
        "enabled",
        "auto_start",
        "polling_interval_ms",
        "max_concurrent_agents",
        "max_concurrent_agents_by_state",
        "max_turns",
        "max_retry_backoff_ms",
        "failure_retry_base_ms",
    ):
        if key not in raw:
            continue
        value = raw[key]
        if key in {"enabled", "auto_start"}:
            if not isinstance(value, bool):
                raise AgentHarnessConfigError(f"{key} must be a boolean")
            result[key] = value
        elif key == "max_concurrent_agents_by_state":
            if not isinstance(value, dict) or len(value) > 32:
                raise AgentHarnessConfigError(f"{key} must be an object")
            by_state: dict[str, int] = {}
            for state, limit in value.items():
                state_name = _validate_harness_text(state, field_name="state", max_length=64).lower()
                if not re.fullmatch(r"[a-z0-9_-]+", state_name):
                    raise AgentHarnessConfigError("Invalid state name")
                by_state[state_name] = _validate_harness_int(
                    limit, field_name=f"{key}.{state_name}", minimum=1, maximum=32
                )
            result[key] = by_state
        else:
            limits = {
                "polling_interval_ms": (1, 86_400_000),
                "max_concurrent_agents": (1, 32),
                "max_turns": (1, 200),
                "max_retry_backoff_ms": (1, 86_400_000),
                "failure_retry_base_ms": (1, 86_400_000),
            }
            minimum, maximum = limits[key]
            result[key] = _validate_harness_int(
                value, field_name=key, minimum=minimum, maximum=maximum
            )

    tracker = raw.get("tracker")
    if tracker is not None:
        if not isinstance(tracker, dict):
            raise AgentHarnessConfigError("tracker must be an object")
        unknown_tracker = set(tracker) - AGENT_HARNESS_TRACKER_UPDATE_KEYS
        if unknown_tracker:
            raise AgentHarnessConfigError(
                f"Unsupported tracker setting: {sorted(str(item) for item in unknown_tracker)[0]}"
            )
        normalized_tracker: dict[str, Any] = {}
        for key in ("active_states", "terminal_states"):
            if key in tracker:
                normalized_tracker[key] = _validate_harness_string_list(
                    tracker[key], field_name=f"tracker.{key}"
                )
        if "include_all_active_tasks" in tracker:
            if not isinstance(tracker["include_all_active_tasks"], bool):
                raise AgentHarnessConfigError("tracker.include_all_active_tasks must be a boolean")
            normalized_tracker["include_all_active_tasks"] = tracker["include_all_active_tasks"]
        if "project_id" in tracker:
            project_id = tracker["project_id"]
            if project_id is not None:
                project_id = _validate_harness_text(project_id, field_name="tracker.project_id", max_length=128)
            normalized_tracker["project_id"] = project_id
        result["tracker"] = normalized_tracker

    codex = raw.get("codex")
    if codex is not None:
        if not isinstance(codex, dict):
            raise AgentHarnessConfigError("codex must be an object")
        unknown_codex = set(codex) - AGENT_HARNESS_CODEX_UPDATE_KEYS
        if unknown_codex:
            raise AgentHarnessConfigError(
                f"Unsupported codex setting: {sorted(str(item) for item in unknown_codex)[0]}"
            )
        normalized_codex: dict[str, Any] = {}
        for key in ("model", "reasoning_effort"):
            if key in codex:
                value = codex[key]
                if value is not None:
                    value = _validate_harness_text(value, field_name=f"codex.{key}", max_length=256)
                normalized_codex[key] = value
        if "approval_policy" in codex:
            approval = _validate_harness_text(codex["approval_policy"], field_name="codex.approval_policy", max_length=32).lower()
            if approval not in AGENT_HARNESS_SAFE_APPROVAL_POLICIES:
                raise AgentHarnessConfigError("Unsupported codex approval policy")
            normalized_codex["approval_policy"] = approval
        if "exec_sandbox" in codex:
            sandbox = _validate_harness_text(codex["exec_sandbox"], field_name="codex.exec_sandbox", max_length=32).lower()
            if sandbox not in AGENT_HARNESS_SAFE_SANDBOXES:
                raise AgentHarnessConfigError("Unsupported codex sandbox")
            normalized_codex["exec_sandbox"] = sandbox
        if "runner" in codex:
            runner = _validate_harness_text(codex["runner"], field_name="codex.runner", max_length=32).lower().replace("-", "_")
            if runner not in AGENT_HARNESS_SAFE_RUNNERS:
                raise AgentHarnessConfigError("Unsupported Agent Harness runner")
            normalized_codex["runner"] = runner
        if "stall_timeout_ms" in codex:
            normalized_codex["stall_timeout_ms"] = _validate_harness_int(
                codex["stall_timeout_ms"], field_name="codex.stall_timeout_ms", minimum=0, maximum=86_400_000
            )
        result["codex"] = normalized_codex

    claude = raw.get("claude")
    if claude is not None:
        if not isinstance(claude, dict):
            raise AgentHarnessConfigError("claude must be an object")
        unknown_claude = set(claude) - AGENT_HARNESS_CLAUDE_UPDATE_KEYS
        if unknown_claude:
            raise AgentHarnessConfigError(
                f"Unsupported claude setting: {sorted(str(item) for item in unknown_claude)[0]}"
            )
        normalized_claude: dict[str, Any] = {}
        for key in ("model", "reasoning_effort"):
            if key in claude:
                value = claude[key]
                if value is not None:
                    value = _validate_harness_text(value, field_name=f"claude.{key}", max_length=256)
                normalized_claude[key] = value
        result["claude"] = normalized_claude

    # Current UI aliases are intentionally converted to the same safe codex
    # branch used by the explicit schema.  No executable/path field is exposed.
    aliases = {key: raw[key] for key in ("runner", "model", "effort") if key in raw}
    if aliases:
        codex_result = result.setdefault("codex", {})
        if "runner" in aliases:
            runner = _validate_harness_text(aliases["runner"], field_name="runner", max_length=32).lower().replace("-", "_")
            if runner not in AGENT_HARNESS_SAFE_RUNNERS:
                raise AgentHarnessConfigError("Unsupported Agent Harness runner")
            codex_result["runner"] = runner
        if "model" in aliases:
            codex_result["model"] = _validate_harness_text(aliases["model"], field_name="model", max_length=256) if aliases["model"] else None
        if "effort" in aliases:
            codex_result["reasoning_effort"] = _validate_harness_text(aliases["effort"], field_name="effort", max_length=32) if aliases["effort"] else None
    return result


def public_agent_harness_settings(raw: Any) -> dict[str, Any]:
    """Project deployment-owned config to the authenticated settings view."""

    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "enabled",
        "auto_start",
        "polling_interval_ms",
        "max_concurrent_agents",
        "max_concurrent_agents_by_state",
        "max_turns",
        "max_retry_backoff_ms",
        "failure_retry_base_ms",
    ):
        if key in raw:
            result[key] = copy.deepcopy(raw[key])
    tracker = raw.get("tracker") if isinstance(raw.get("tracker"), dict) else {}
    if tracker:
        result["tracker"] = {
            key: copy.deepcopy(tracker[key])
            for key in ("active_states", "terminal_states", "include_all_active_tasks", "project_id")
            if key in tracker
        }
    codex = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    claude = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
    if codex:
        result["runner"] = str(codex.get("runner") or "codex_exec")
        result["model"] = str(codex.get("model") or "")
        result["effort"] = str(codex.get("reasoning_effort") or "")
    elif claude:
        result["runner"] = "claude_code"
        result["model"] = str(claude.get("model") or "")
        result["effort"] = str(claude.get("reasoning_effort") or "")
    return result


def strip_agent_harness_secret_keys(value: Any) -> Any:
    """Remove credential-shaped keys from legacy persisted harness data."""

    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _HARNESS_SECRET_KEYS:
                continue
            result[key] = strip_agent_harness_secret_keys(child)
        return result
    if isinstance(value, list):
        return [strip_agent_harness_secret_keys(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_agent_harness_secret_keys(item) for item in value)
    return value


def _safe_cli_bin(value: Any, *, default: str, env_name: str) -> str:
    """Resolve a deployment-owned CLI path without accepting shell syntax."""

    candidate = value
    if candidate in (None, ""):
        candidate = os.getenv(env_name) or default
    if not isinstance(candidate, str):
        return default
    candidate = candidate.strip()
    if not candidate or _HARNESS_SHELL_META.search(candidate) or any(char.isspace() for char in candidate):
        return default
    # Keep the documented executable names and absolute paths that point to a
    # file named after the expected CLI.  ``/bin/sh`` or an arbitrary uploaded
    # executable cannot become the harness runner through malformed config.
    basename = Path(candidate).name.lower()
    if basename not in {default.lower(), f"{default}.exe"}:
        return default
    return candidate


def _safe_hook_command(value: Any) -> str | None:
    """Keep only simple, non-shell hook commands from trusted config files.

    The public update endpoint rejects hooks entirely.  This second boundary
    protects startup when an old/malformed config file contains shell syntax;
    direct ``run_hook`` callers remain available for existing in-process use.
    """

    if value in (None, ""):
        return None
    if not isinstance(value, str) or _HARNESS_SHELL_META.search(value):
        return None
    try:
        parts = shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts or len(parts) > 16:
        return None
    if "/" in parts[0] or "\\" in parts[0] or parts[0] in {".", ".."}:
        return None
    executable = Path(parts[0]).name.lower()
    if executable in {
        "sh",
        "bash",
        "zsh",
        "cmd",
        "powershell",
        "pwsh",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "rm",
        "del",
        "touch",
        "mv",
        "cp",
        "chmod",
        "chown",
        "mkdir",
        "rmdir",
        "curl",
        "wget",
        "nc",
        "netcat",
        "ssh",
        "scp",
        "git",
    }:
        return None
    return value.strip()


def _safe_custom_command(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or _HARNESS_SHELL_META.search(value):
        return None
    try:
        parts = shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts or len(parts) > 16:
        return None
    if "/" in parts[0] or "\\" in parts[0] or parts[0] in {".", ".."}:
        return None
    executable = Path(parts[0]).name.lower()
    if executable in {
        "sh",
        "bash",
        "zsh",
        "cmd",
        "powershell",
        "pwsh",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "rm",
        "del",
        "touch",
        "mv",
        "cp",
        "chmod",
        "chown",
        "mkdir",
        "rmdir",
        "curl",
        "wget",
        "nc",
        "netcat",
        "ssh",
        "scp",
        "git",
    }:
        return None
    return value.strip()


def _safe_workspace_value(value: Any, *, default: str, max_length: int) -> str:
    """Normalize git/workspace metadata used as argv values.

    These values are not public API fields, but rejecting control characters,
    whitespace, and option-like prefixes prevents a malformed persisted
    config from changing the meaning of the git subprocess invocation.
    """

    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > max_length
        or candidate.startswith("-")
        or _HARNESS_SHELL_META.search(candidate)
        or any(char.isspace() for char in candidate)
    ):
        return default
    return candidate


def _safe_model_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 256
        or any(ord(char) < 32 for char in candidate)
        or _HARNESS_SHELL_META.search(candidate)
        or any(char in candidate for char in "\"'")
    ):
        return None
    return candidate


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
    raw = value if isinstance(value, str) and value else default
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
        # A malformed top-level value must fail closed to defaults rather than
        # allowing ``.get`` calls below to turn into an initialization error.
        if not isinstance(raw, dict):
            raw = {}

        tracker_raw = raw.get("tracker", {}) or {}
        hooks_raw = raw.get("hooks", {}) or {}
        codex_raw = raw.get("codex", {}) or {}
        claude_raw = raw.get("claude", {}) or {}
        custom_command_raw = raw.get("custom_command", {}) or {}
        if not isinstance(tracker_raw, dict):
            tracker_raw = {}
        if not isinstance(hooks_raw, dict):
            hooks_raw = {}
        if not isinstance(codex_raw, dict):
            codex_raw = {}
        if not isinstance(claude_raw, dict):
            claude_raw = {}
        if not isinstance(custom_command_raw, dict):
            custom_command_raw = {}
        codex_cli_raw = _config_get(config, "codex_cli", {}) or {}
        claude_cli_raw = _config_get(config, "claude_cli", {}) or {}
        if not isinstance(codex_cli_raw, dict):
            codex_cli_raw = {}
        if not isinstance(claude_cli_raw, dict):
            claude_cli_raw = {}
        by_state_raw = raw.get("max_concurrent_agents_by_state", {}) or {}
        if not isinstance(by_state_raw, dict):
            by_state_raw = {}
        by_state = {
            _normalize_state(str(state)): int(limit)
            for state, limit in by_state_raw.items()
            if _is_positive_int(limit)
        }

        raw_runner = raw.get("runner") or codex_raw.get("runner") or "codex_exec"
        raw_model = raw.get("model") or codex_raw.get("model")
        raw_effort = raw.get("effort") or codex_raw.get("reasoning_effort")
        approval_policy = codex_raw.get("approval_policy", "never")
        if not isinstance(approval_policy, str) or approval_policy.strip().lower() not in AGENT_HARNESS_SAFE_APPROVAL_POLICIES:
            approval_policy = "never"
        exec_sandbox = str(codex_raw.get("exec_sandbox") or "workspace-write").strip().lower()
        if exec_sandbox not in AGENT_HARNESS_SAFE_SANDBOXES:
            exec_sandbox = "workspace-write"
        normalized_runner = _normalize_runner(str(raw_runner))
        # ``custom_command`` is retained for trusted, existing deployments,
        # but malformed shell syntax is disabled before a runner is built.
        custom_command = _safe_custom_command(custom_command_raw.get("command"))
        custom_args = _as_str_list(custom_command_raw.get("args"), [])
        if len(custom_args) > 32 or any(_HARNESS_SHELL_META.search(arg) for arg in custom_args):
            custom_args = []

        return cls(
            # Agent Harness is an independent background automation feature.
            # Its lifecycle must not depend on Agent Team delegation or any
            # Team/Subagent route, including routes persisted by older config
            # versions.
            enabled=_as_bool(raw.get("enabled"), False),
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
            workspace_base_ref=_safe_workspace_value(raw.get("workspace_base_ref"), default="origin/main", max_length=256),
            workspace_branch_prefix=_safe_workspace_value(raw.get("workspace_branch_prefix"), default="harness/", max_length=64),
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
                after_create=_safe_hook_command(hooks_raw.get("after_create")),
                before_run=_safe_hook_command(hooks_raw.get("before_run")),
                after_run=_safe_hook_command(hooks_raw.get("after_run")),
                before_remove=_safe_hook_command(hooks_raw.get("before_remove")),
                timeout_ms=_as_int(hooks_raw.get("timeout_ms"), 60_000, minimum=1),
            ),
            codex=HarnessCodexSettings(
                bin_path=_safe_cli_bin(codex_raw.get("bin_path"), default="codex", env_name="CODEX_BIN"),
                model=(
                    _safe_model_value(raw_model)
                    or _safe_model_value(codex_cli_raw.get("model"))
                ),
                reasoning_effort=(
                    _safe_model_value(raw_effort)
                    or _safe_model_value(codex_cli_raw.get("reasoning_effort"))
                ),
                approval_policy=approval_policy,
                exec_sandbox=exec_sandbox,
                stall_timeout_ms=_as_int(codex_raw.get("stall_timeout_ms"), 300_000, minimum=0),
                runner=normalized_runner,
            ),
            claude=HarnessClaudeSettings(
                bin_path=_safe_cli_bin(claude_raw.get("bin_path"), default="claude", env_name="CLAUDE_BIN"),
                model=(
                    _safe_model_value(claude_raw.get("model"))
                    or _safe_model_value(claude_cli_raw.get("model"))
                ),
                reasoning_effort=(
                    _safe_model_value(claude_raw.get("reasoning_effort"))
                    or _safe_model_value(claude_cli_raw.get("reasoning_effort"))
                ),
            ),
            custom_command=HarnessCustomCommandSettings(
                command=custom_command,
                args=custom_args,
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
