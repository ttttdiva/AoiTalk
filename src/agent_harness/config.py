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
        # Legacy flat aliases.  ``runner`` is stored at ``codex.runner`` (the
        # runner selector read by ``build_runner``); ``model``/``effort`` are
        # routed to the branch of the selected runner (``codex`` or
        # ``claude``) and are never persisted as top-level options.
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
AGENT_HARNESS_CLAUDE_UPDATE_KEYS = frozenset({"model", "reasoning_effort", "permission_mode"})
AGENT_HARNESS_SAFE_RUNNERS = frozenset(
    {"codex_exec", "codex_cli", "claude_code", "claude_cli", "custom_command"}
)
AGENT_HARNESS_CLAUDE_RUNNERS = frozenset({"claude_code", "claude_cli"})
# Only these aliases are unambiguously Claude-owned in the pre-branch config.
# A generic value such as ``high`` is not enough evidence to reinterpret a
# Codex branch as Claude; effort is migrated only together with one of these
# recognized Claude model values.
LEGACY_CLAUDE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})
# Legacy Claude permission modes remain accepted in saved settings, but
# the runner always passes --dangerously-skip-permissions. These values
# no longer control the CLI, and AoiTalk scope enforcement stays separate.
AGENT_HARNESS_SAFE_CLAUDE_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk"}
)
AGENT_HARNESS_DEFAULT_CLAUDE_PERMISSION_MODE = "acceptEdits"
AGENT_HARNESS_SAFE_SANDBOXES = frozenset({"read-only", "workspace-write"})
AGENT_HARNESS_SAFE_EXECUTION_BACKENDS = frozenset({"wsl_bwrap", "wsl2_bwrap"})
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


def _normalize_runner_value(value: Any, *, field_name: str) -> str:
    runner = _validate_harness_text(value, field_name=field_name, max_length=32).lower().replace("-", "_")
    if runner not in AGENT_HARNESS_SAFE_RUNNERS:
        raise AgentHarnessConfigError("Unsupported Agent Harness runner")
    return runner


def _optional_harness_text(value: Any, *, field_name: str, max_length: int) -> str | None:
    """``None``/empty string clears the value; anything else is validated."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _validate_harness_text(value, field_name=field_name, max_length=max_length)


def is_claude_runner(runner: Any) -> bool:
    return str(runner or "").strip().lower().replace("-", "_") in AGENT_HARNESS_CLAUDE_RUNNERS


def validate_agent_harness_update(
    payload: Any,
    *,
    current_runner: str | None = None,
) -> dict[str, Any]:
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
                normalized_codex[key] = _optional_harness_text(
                    codex[key], field_name=f"codex.{key}", max_length=256
                )
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
            normalized_codex["runner"] = _normalize_runner_value(
                codex["runner"], field_name="codex.runner"
            )
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
                normalized_claude[key] = _optional_harness_text(
                    claude[key], field_name=f"claude.{key}", max_length=256
                )
        if "permission_mode" in claude:
            mode = _optional_harness_text(
                claude["permission_mode"], field_name="claude.permission_mode", max_length=32
            )
            if mode is not None and mode not in AGENT_HARNESS_SAFE_CLAUDE_PERMISSION_MODES:
                raise AgentHarnessConfigError("Unsupported claude permission mode")
            normalized_claude["permission_mode"] = mode
        result["claude"] = normalized_claude

    # Flat aliases.  The runner selector keeps its canonical location
    # (``codex.runner``); model/effort go to the selected runner's own branch
    # so choosing Claude Code never overwrites the Codex model (and vice
    # versa).  Explicit branch values win over aliases.
    if "runner" in raw:
        runner = _normalize_runner_value(raw["runner"], field_name="runner")
        codex_branch = result.setdefault("codex", {})
        if "runner" in codex_branch and codex_branch["runner"] != runner:
            raise AgentHarnessConfigError("Conflicting Agent Harness runner values")
        codex_branch["runner"] = runner
    if "model" in raw or "effort" in raw:
        effective_runner = (
            (result.get("codex") or {}).get("runner")
            or current_runner
            or "codex_exec"
        )
        target = "claude" if is_claude_runner(effective_runner) else "codex"
        branch = result.setdefault(target, {})
        if "model" in raw and "model" not in branch:
            branch["model"] = _optional_harness_text(raw["model"], field_name="model", max_length=256)
        if "effort" in raw and "reasoning_effort" not in branch:
            branch["reasoning_effort"] = _optional_harness_text(
                raw["effort"], field_name="effort", max_length=32
            )
    return result


def persisted_agent_harness_runner(raw: Any) -> str:
    """Return the runner selected by a persisted ``agent_harness`` mapping."""

    if not isinstance(raw, dict):
        return "codex_exec"
    codex = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    claude = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
    runner = raw.get("runner") or codex.get("runner")
    if not runner and claude and not codex:
        runner = "claude_code"
    return _normalize_runner(str(runner or "codex_exec"))


def _is_legacy_claude_model(value: Any) -> bool:
    model = _safe_model_value(value)
    if not model:
        return False
    base = model.split("[", 1)[0].strip().lower()
    return base in LEGACY_CLAUDE_MODEL_ALIASES or base.startswith("claude-")


def legacy_claude_agent_harness_settings(raw: Any) -> dict[str, str]:
    """Project pre-branch Claude settings without guessing new Codex data."""

    if not isinstance(raw, dict):
        return {}
    if not is_claude_runner(persisted_agent_harness_runner(raw)):
        return {}
    codex = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    claude = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
    result: dict[str, str] = {}
    legacy_model = _safe_model_value(raw.get("model")) or _safe_model_value(codex.get("model"))
    if not _is_legacy_claude_model(legacy_model):
        return result
    if not _safe_model_value(claude.get("model")) and legacy_model:
        result["model"] = legacy_model
    legacy_effort = _safe_model_value(raw.get("effort")) or _safe_model_value(codex.get("reasoning_effort"))
    if not _safe_model_value(claude.get("reasoning_effort")) and legacy_effort:
        result["reasoning_effort"] = legacy_effort
    return result


def migrate_legacy_claude_agent_harness_config(raw: Any) -> dict[str, Any]:
    """Canonicalize legacy Claude-in-Codex settings on the next admin save."""

    if not isinstance(raw, dict):
        return {}
    result = copy.deepcopy(raw)
    legacy = legacy_claude_agent_harness_settings(result)
    if not legacy:
        return result
    claude = result.setdefault("claude", {})
    if not isinstance(claude, dict):
        claude = {}
        result["claude"] = claude
    claude.update({key: value for key, value in legacy.items() if not claude.get(key)})
    # Remove only the ambiguous legacy aliases. A later Codex value is then
    # written independently by the runner-specific branch.
    result.pop("model", None)
    result.pop("effort", None)
    codex = result.get("codex")
    if isinstance(codex, dict):
        codex.pop("model", None)
        codex.pop("reasoning_effort", None)
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
    runner = persisted_agent_harness_runner(raw)
    legacy = legacy_claude_agent_harness_settings(raw)
    codex_view = {
        "model": str(codex.get("model") or ""),
        "reasoning_effort": str(codex.get("reasoning_effort") or ""),
    }
    claude_view = {
        "model": str(claude.get("model") or legacy.get("model") or ""),
        "reasoning_effort": str(claude.get("reasoning_effort") or legacy.get("reasoning_effort") or ""),
        "permission_mode": str(
            claude.get("permission_mode") or AGENT_HARNESS_DEFAULT_CLAUDE_PERMISSION_MODE
        ),
    }
    active = claude_view if is_claude_runner(runner) else codex_view
    result["runner"] = runner
    result["codex"] = codex_view
    result["claude"] = claude_view
    # Flat aliases always describe the selected runner's own branch.
    result["model"] = active["model"]
    result["effort"] = active["reasoning_effort"]
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
    permission_mode: str = AGENT_HARNESS_DEFAULT_CLAUDE_PERMISSION_MODE
    # Deployment-owned permission rules passed to ``--allowedTools`` (for
    # example ``Bash(npm test)``).  Not part of the HTTP update contract.
    allowed_tools: list[str] = field(default_factory=list)


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
    # Deployment-owned Enterprise execution policy.  These values are kept
    # out of the HTTP update allowlist above: an API caller must not be able to
    # select a host process backend or enlarge the server-issued capability.
    execution_enabled: bool = True
    execution_backend: str = "wsl_bwrap"
    execution_network: str = "none"
    execution_resource_limits: dict[str, Any] = field(default_factory=dict)
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
        execution_raw = raw.get("execution", {}) or {}
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
        if not isinstance(execution_raw, dict):
            execution_raw = {}
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
        normalized_runner = _normalize_runner(str(raw_runner))
        legacy_claude = legacy_claude_agent_harness_settings(raw)
        raw_model = raw.get("model") or codex_raw.get("model")
        raw_effort = raw.get("effort") or codex_raw.get("reasoning_effort")
        approval_policy = codex_raw.get("approval_policy", "never")
        if not isinstance(approval_policy, str) or approval_policy.strip().lower() not in AGENT_HARNESS_SAFE_APPROVAL_POLICIES:
            approval_policy = "never"
        exec_sandbox = str(codex_raw.get("exec_sandbox") or "workspace-write").strip().lower()
        if exec_sandbox not in AGENT_HARNESS_SAFE_SANDBOXES:
            exec_sandbox = "workspace-write"
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
            execution_enabled=_as_bool(execution_raw.get("enabled"), True),
            execution_backend=str(
                execution_raw.get("backend") or "wsl_bwrap"
            ).strip().lower().replace("-", "_"),
            execution_network=str(
                execution_raw.get("network")
                or execution_raw.get("network_capability")
                or "none"
            ).strip().lower(),
            execution_resource_limits=(
                dict(execution_raw.get("resource_limits"))
                if isinstance(execution_raw.get("resource_limits"), dict)
                else {}
            ),
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
                    (
                        None
                        if legacy_claude.get("model")
                        else _safe_model_value(raw_model)
                    )
                    or _safe_model_value(codex_cli_raw.get("model"))
                ),
                reasoning_effort=(
                    (
                        None
                        if legacy_claude.get("reasoning_effort")
                        else _safe_model_value(raw_effort)
                    )
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
                    or legacy_claude.get("model")
                    or _safe_model_value(claude_cli_raw.get("model"))
                ),
                reasoning_effort=(
                    _safe_model_value(claude_raw.get("reasoning_effort"))
                    or legacy_claude.get("reasoning_effort")
                    or _safe_model_value(claude_cli_raw.get("reasoning_effort"))
                ),
                permission_mode=_safe_claude_permission_mode(claude_raw.get("permission_mode")),
                allowed_tools=_safe_claude_allowed_tools(claude_raw.get("allowed_tools")),
            ),
            custom_command=HarnessCustomCommandSettings(
                command=custom_command,
                args=custom_args,
            ),
        )


def _safe_claude_permission_mode(value: Any) -> str:
    if isinstance(value, str) and value.strip() in AGENT_HARNESS_SAFE_CLAUDE_PERMISSION_MODES:
        return value.strip()
    return AGENT_HARNESS_DEFAULT_CLAUDE_PERMISSION_MODE


def _safe_claude_allowed_tools(value: Any) -> list[str]:
    items = _as_str_list(value, [])
    if len(items) > 32:
        return []
    return [
        item
        for item in items
        if len(item) <= 256
        and not item.startswith("-")
        and not any(ord(char) < 32 for char in item)
        and not _HARNESS_SHELL_META.search(item)
    ]


def _is_positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _normalize_state(state: str) -> str:
    return state.strip().lower()


def _normalize_runner(runner: str) -> str:
    return runner.strip().lower().replace("-", "_") or "codex_exec"
