"""Durable, bounded local-LLM agent tasks for Home MCP Gateway delegation.

A remote MCP caller (ChatGPT through Home MCP Gateway) submits one high-level
goal.  AoiTalk then runs the multi-step file / shell / git / build loop locally
with the configured ``openai_compatible_local`` model, so the caller only
polls ``status`` / ``tail`` instead of issuing dozens of MCP tool calls.

The module deliberately reuses the existing runtime pieces instead of adding a
second agent implementation:

* the provider client comes from :func:`create_llm_client_for_target`
  (managed llama-server lease, privacy gateway, profile request wire);
* the tool loop is :func:`run_openai_compatible_turn_loop` with the ordinary
  ``RegistryToolRouter`` and tool policy;
* tools are the existing ``os_operations`` functions.  File tools run inside
  an :class:`AgentRunScope` bound to the requested workspace.

Host commands cannot be confined by a text check (see ``command_executor``),
so this module keeps them opt-in, forces their cwd into the workspace, denies
destructive / branch-changing / secret-reading command text and records a
deterministic before/after Git comparison.  It is a bounded automation
surface for the local operator, not a sandbox.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..security.sensitive_files import (
    SENSITIVE_FILE_PATTERNS,
    is_sensitive_file_name,
    is_sensitive_relative_path,
)
from ..llm.codex_instruction_review import ReviewedHostInstructions
from ..llm.codex_task_completion import CodexTaskProtocolError
from ..utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)

TASK_STATES = ("queued", "running", "waiting", "waiting_approval", "interrupted", "completed", "failed", "cancelled")
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
MODES = ("read_only", "mutate")
TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")
REPORT_STATUSES = frozenset({"completed", "failed", "blocked"})
CHECK_VALUES = frozenset({"pass", "fail", "skipped", "not_run", "unknown"})

_REPO_ROOT = Path(__file__).resolve().parents[2]


class LocalAgentTaskError(Exception):
    """Validation/lookup error with a stable, non-sensitive code."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class _TaskStop(Exception):
    """Internal control flow that ends the tool loop at a safe boundary."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now() -> float:
    return time.time()


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = None
        if value is not None:
            return value
        if isinstance(config, Mapping):
            node: Any = config
            for part in key.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    return default
                node = node[part]
            return default if node is None else node
    return default


def _int_cfg(config: Any, key: str, default: int) -> int:
    try:
        return int(_cfg(config, key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------

_SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|CREDENTIAL|AUTH|PRIVATE)"
)
_SECRET_TEXT_RES = (
    re.compile(
        r"(?i)\b((?:[A-Za-z0-9_]*?)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
        r"secret|password|passwd|credential|cookie|authorization))(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk|ghp|gho|ghs|github_pat|xox[baprs]|aoitpat)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SECRET_FILE_RES = SENSITIVE_FILE_PATTERNS
_SECRET_PATH_TOKEN_RE = re.compile(
    r"(?i)(?:^|[\s\"'`=/\\(:;|&<>])(\.env(?:\.(?!example\b|sample\b|template\b|dist\b)[A-Za-z0-9_-]+)?"
    r"|[^\s\"'`/\\]+\.(?:pem|key|p12|pfx|jks|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|\.git-credentials|\.netrc|\.npmrc|\.pypirc)(?=$|[\s\"'`;|&<>)])"
)


class SecretRedactor:
    """Redact credential-looking text and the literal values of secret env vars."""

    def __init__(self, extra_values: Iterable[str] = ()):
        values = set()
        for name, value in os.environ.items():
            if value and len(value) >= 8 and _SECRET_ENV_NAME_RE.search(name):
                values.add(value)
        for value in extra_values:
            if isinstance(value, str) and len(value) >= 8:
                values.add(value)
        # Longest first so an overlapping shorter value cannot leave a suffix.
        self._values = sorted(values, key=len, reverse=True)

    def text(self, value: Any, limit: int | None = None) -> str:
        text = value if isinstance(value, str) else str(value)
        for secret in self._values:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_TEXT_RES:
            if pattern.groups >= 3:
                text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        if limit is not None and len(text) > limit:
            head = max(0, limit - 200)
            text = text[:head] + f"\n...[truncated {len(text) - limit} chars]...\n" + text[-150:]
        return text

    def value(self, value: Any, limit: int = 20_000) -> Any:
        if isinstance(value, str):
            return self.text(value, limit)
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if isinstance(key, str) and _SECRET_ENV_NAME_RE.search(key) and isinstance(item, str) and item:
                    result[key] = "[REDACTED]"
                else:
                    result[str(key)] = self.value(item, limit)
            return result
        if isinstance(value, (list, tuple)):
            return [self.value(item, limit) for item in list(value)[:500]]
        return value


def is_secret_file_name(path: str) -> bool:
    return is_sensitive_file_name(path)


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalAgentTaskLimits:
    default_max_tool_rounds: int = 40
    max_max_tool_rounds: int = 120
    default_timeout_sec: int = 1800
    max_timeout_sec: int = 7200
    default_command_timeout_sec: int = 300
    max_command_timeout_sec: int = 1800
    max_concurrency: int = 1
    max_background_jobs: int = 3
    repeat_limit: int = 3
    failure_streak_limit: int = 8
    max_events: int = 4000
    tool_result_chars: int = 12_000

    @classmethod
    def from_config(cls, config: Any) -> "LocalAgentTaskLimits":
        base = cls()
        values = {}
        for item in dataclasses.fields(cls):
            values[item.name] = max(1, _int_cfg(config, f"local_agent_tasks.{item.name}", getattr(base, item.name)))
        return cls(**values)


@dataclass(frozen=True)
class LocalAgentTaskSpec:
    goal: str
    workspace: str
    constraints: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    mode: str = "read_only"
    allow_commands: bool = True
    allow_adb: bool = False
    allow_git_commit: bool = False
    allow_git_push: bool = False
    max_tool_rounds: int = 40
    timeout_sec: int = 1800
    command_timeout_sec: int = 300
    expected_branch: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    codex_connection: str | None = None
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["constraints"] = list(self.constraints)
        data["completion_criteria"] = list(self.completion_criteria)
        # Preserve fingerprints of pre-route requests across upgrades.
        for key in ("provider", "model", "effort", "codex_connection", "agent_id"):
            if data[key] is None:
                data.pop(key)
        # Push is never a local-task request field (validation rejects it), so a
        # stored request must round-trip without it and keep older fingerprints.
        if not data["allow_git_push"]:
            data.pop("allow_git_push")
        return data

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


def default_allowed_roots(config: Any) -> list[Path]:
    configured = _cfg(config, "local_agent_tasks.allowed_roots", None)
    if isinstance(configured, str):
        configured = [configured]
    roots: list[Path] = []
    if isinstance(configured, (list, tuple)) and configured:
        candidates = [str(item) for item in configured if str(item or "").strip()]
    else:
        # Default: sibling repositories of this checkout (e.g. D:\Dev\*).
        candidates = [str(_REPO_ROOT.parent)]
    for candidate in candidates:
        try:
            path = Path(os.path.realpath(os.path.abspath(os.path.expanduser(candidate))))
        except (OSError, ValueError):
            continue
        if path.is_dir():
            roots.append(path)
    return roots


def _path_within(path: Path, root: Path) -> bool:
    try:
        a = os.path.normcase(os.path.normpath(str(path)))
        b = os.path.normcase(os.path.normpath(str(root)))
        return os.path.commonpath((a, b)) == b
    except (OSError, ValueError):
        return False


def _text_list(value: Any, name: str, *, max_items: int = 20, max_chars: int = 1000) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or len(value) > max_items:
        raise LocalAgentTaskError("invalid_input", f"{name} must be a list of at most {max_items} strings")
    items = []
    for item in value:
        if not isinstance(item, str) or len(item) > max_chars:
            raise LocalAgentTaskError("invalid_input", f"{name} items must be strings up to {max_chars} chars")
        if item.strip():
            items.append(item.strip())
    return tuple(items)


def _bounded_int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise LocalAgentTaskError("invalid_input", f"{name} must be an integer")
    value = int(value)
    if not low <= value <= high:
        raise LocalAgentTaskError("invalid_input", f"{name} must be between {low} and {high}")
    return value


def _bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise LocalAgentTaskError("invalid_input", f"{name} must be a boolean")
    return value


def validate_task_request(
    payload: Mapping[str, Any],
    *,
    allowed_roots: Sequence[Path],
    limits: LocalAgentTaskLimits,
    route_expectations: bool = False,
) -> LocalAgentTaskSpec:
    if not isinstance(payload, Mapping):
        raise LocalAgentTaskError("invalid_input", "request body must be an object")
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
        raise LocalAgentTaskError("invalid_input", "goal must be a non-empty string up to 8000 chars")
    raw_workspace = payload.get("workspace") or payload.get("repository")
    if not isinstance(raw_workspace, str) or not raw_workspace.strip() or len(raw_workspace) > 1024:
        raise LocalAgentTaskError("invalid_workspace", "workspace must be an absolute directory path")
    if not os.path.isabs(raw_workspace):
        raise LocalAgentTaskError("invalid_workspace", "workspace must be an absolute directory path")
    try:
        workspace = Path(os.path.realpath(os.path.abspath(raw_workspace)))
    except (OSError, ValueError):
        raise LocalAgentTaskError("invalid_workspace", "workspace could not be resolved") from None
    if not workspace.is_dir():
        raise LocalAgentTaskError("invalid_workspace", "workspace does not exist or is not a directory")
    if workspace.parent == workspace:
        raise LocalAgentTaskError("invalid_workspace", "a filesystem root cannot be a task workspace")
    if not any(_path_within(workspace, root) for root in allowed_roots):
        raise LocalAgentTaskError(
            "workspace_not_allowed",
            "workspace is outside local_agent_tasks.allowed_roots",
            http_status=403,
        )
    mode = payload.get("mode", "read_only")
    if mode not in MODES:
        raise LocalAgentTaskError("invalid_input", "mode must be read_only or mutate")
    expected_branch = payload.get("expected_branch")
    if expected_branch is not None and (not isinstance(expected_branch, str) or not BRANCH_RE.fullmatch(expected_branch)):
        raise LocalAgentTaskError("invalid_input", "expected_branch is invalid")
    allow_git_commit = _bool(payload.get("allow_git_commit"), "allow_git_commit", False)
    if allow_git_commit and mode != "mutate":
        raise LocalAgentTaskError("invalid_input", "allow_git_commit requires mode=mutate")
    known = {
        "goal", "workspace", "repository", "constraints", "completion_criteria", "mode",
        "allow_commands", "allow_adb", "allow_git_commit", "max_tool_rounds", "timeout_sec",
        "command_timeout_sec", "expected_branch", "idempotency_key", "provider", "model", "effort", "codex_connection", "agent_id",
    }
    unknown = sorted(str(key) for key in payload.keys() if key not in known)
    if unknown:
        raise LocalAgentTaskError("invalid_input", "unknown fields: " + ", ".join(unknown[:10]))
    agent_id = payload.get("agent_id")
    if agent_id is not None:
        try:
            agent_id = str(uuid.UUID(str(agent_id)))
        except (ValueError, TypeError, AttributeError):
            raise LocalAgentTaskError("invalid_input", "agent_id must be a UUID") from None
    route = {name: payload.get(name) for name in ("provider", "model", "effort")}
    connection = payload.get("codex_connection")
    if connection is not None and (connection != "app_profile" or payload.get("provider") != "codex-cli"):
        raise LocalAgentTaskError("invalid_task_route", "app_profile requires an explicit Codex route")
    if connection is not None and (mode != "read_only" or payload.get("allow_commands", True)
                                   or payload.get("allow_adb", False) or allow_git_commit):
        raise LocalAgentTaskError("codex_profile_read_only_required", "app profile does not grant mutation or command permissions", 403)
    if route_expectations:
        if route["provider"] is not None and route["provider"] not in {"openai_compatible_local", "ollama", "codex-cli"}:
            raise LocalAgentTaskError("invalid_task_route", "unsupported task provider")
        if route["model"] is not None and (not isinstance(route["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", route["model"])):
            raise LocalAgentTaskError("invalid_task_route", "invalid expected catalog model")
        if route["effort"] is not None and (not isinstance(route["effort"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", route["effort"])):
            raise LocalAgentTaskError("invalid_task_route", "invalid reasoning effort")
    elif any(value is not None for value in route.values()):
        if not isinstance(route["provider"], str) or route["provider"] not in {"openai_compatible_local", "ollama", "codex-cli"}:
            raise LocalAgentTaskError("invalid_task_route", "unsupported task provider")
        if not isinstance(route["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", route["model"]):
            raise LocalAgentTaskError("invalid_task_route", "an explicit catalog model is required")
        if route["effort"] is not None and (not isinstance(route["effort"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", route["effort"])):
            raise LocalAgentTaskError("invalid_task_route", "invalid reasoning effort")
    return LocalAgentTaskSpec(
        goal=goal.strip(),
        workspace=str(workspace),
        constraints=_text_list(payload.get("constraints"), "constraints"),
        completion_criteria=_text_list(payload.get("completion_criteria"), "completion_criteria"),
        mode=mode,
        allow_commands=_bool(payload.get("allow_commands"), "allow_commands", True),
        allow_adb=_bool(payload.get("allow_adb"), "allow_adb", False),
        allow_git_commit=allow_git_commit,
        max_tool_rounds=_bounded_int(payload.get("max_tool_rounds"), "max_tool_rounds",
                                     min(limits.default_max_tool_rounds, limits.max_max_tool_rounds),
                                     1, limits.max_max_tool_rounds),
        timeout_sec=_bounded_int(payload.get("timeout_sec"), "timeout_sec",
                                 min(limits.default_timeout_sec, limits.max_timeout_sec), 30, limits.max_timeout_sec),
        command_timeout_sec=_bounded_int(payload.get("command_timeout_sec"), "command_timeout_sec",
                                         min(limits.default_command_timeout_sec, limits.max_command_timeout_sec),
                                         5, limits.max_command_timeout_sec),
        expected_branch=expected_branch,
        agent_id=agent_id,
        codex_connection=connection,
        **route,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class LocalAgentTaskStore:
    """Small JSON store: ``<root>/<task_id>/task.json`` + ``events.jsonl``.

    State survives AoiTalk and Gateway restarts; writes are atomic replaces so
    a reader never observes a torn record.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_keys").mkdir(exist_ok=True)
        self._lock = threading.RLock()

    def _dir(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(str(task_id or "")):
            raise LocalAgentTaskError("not_found", "task not found", http_status=404)
        return self.root / task_id

    @staticmethod
    def _atomic_write(path: Path, data: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def create(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            directory = self._dir(record["task_id"])
            directory.mkdir(parents=False, exist_ok=False)
            self._atomic_write(directory / "task.json", record)
            (directory / "events.jsonl").touch()

    def save(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._atomic_write(self._dir(record["task_id"]) / "task.json", record)

    def load(self, task_id: str) -> dict[str, Any]:
        path = self._dir(task_id) / "task.json"
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise LocalAgentTaskError("not_found", "task not found", http_status=404) from None
            except (OSError, ValueError):
                raise LocalAgentTaskError("corrupt_record", "task record is unreadable", http_status=500) from None
        if not isinstance(data, dict) or data.get("task_id") != task_id:
            raise LocalAgentTaskError("corrupt_record", "task record is unreadable", http_status=500)
        return data

    def ids(self) -> list[str]:
        try:
            return [item.name for item in self.root.iterdir() if item.is_dir() and TASK_ID_RE.fullmatch(item.name)]
        except OSError:
            return []

    def list(self) -> list[dict[str, Any]]:
        records = []
        for task_id in self.ids():
            try:
                records.append(self.load(task_id))
            except LocalAgentTaskError:
                continue
        records.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return records

    def _key_path(self, owner: str, key: str) -> Path:
        digest = hashlib.sha256(f"{owner}\0{key}".encode("utf-8")).hexdigest()
        return self.root / "_keys" / f"{digest}.json"

    def find_key(self, owner: str, key: str) -> Optional[dict[str, Any]]:
        path = self._key_path(owner, key)
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return data if isinstance(data, dict) else None

    def put_key(self, owner: str, key: str, task_id: str, fingerprint: str) -> None:
        with self._lock:
            self._atomic_write(self._key_path(owner, key), {"task_id": task_id, "fingerprint": fingerprint})

    def append_event(self, task_id: str, event: Mapping[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with (self._dir(task_id) / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def tail(self, task_id: str, *, limit: int, max_bytes: int) -> tuple[list[dict[str, Any]], bool]:
        path = self._dir(task_id) / "events.jsonl"
        with self._lock:
            try:
                size = path.stat().st_size
                with path.open("rb") as handle:
                    start = max(0, size - max_bytes)
                    handle.seek(start)
                    raw = handle.read()
            except FileNotFoundError:
                raise LocalAgentTaskError("not_found", "task not found", http_status=404) from None
        lines = raw.decode("utf-8", errors="replace").splitlines()
        truncated = start > 0
        if truncated and lines:
            lines = lines[1:]  # first line may be partial
        events = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        if len(events) > limit:
            truncated = True
            events = events[-limit:]
        return events, truncated


# ---------------------------------------------------------------------------
# Deterministic Git inspection (not delegated to the model)
# ---------------------------------------------------------------------------


def _git(workspace: str, *args: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", workspace, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return completed.returncode, completed.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _file_digest(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def git_snapshot(workspace: str) -> dict[str, Any]:
    code, inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    if code != 0 or inside.strip() != "true":
        return {"is_git": False}
    _, root = _git(workspace, "rev-parse", "--show-toplevel")
    _, branch = _git(workspace, "branch", "--show-current")
    _, head = _git(workspace, "rev-parse", "HEAD")
    code, status = _git(workspace, "status", "--porcelain=v1", "-uall", timeout=60)
    entries: dict[str, str] = {}
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries[path.strip('"')] = line[:2]
    top = Path(root.strip() or workspace)
    digests = {path: _file_digest(top / path) for path in list(entries)[:2000]}
    return {
        "is_git": True,
        "root": str(top),
        "branch": branch.strip() or None,
        "head": head.strip() or None,
        "status_ok": code == 0,
        "entries": entries,
        "digests": digests,
    }


def compare_git_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    if not before.get("is_git") or not after.get("is_git"):
        return {"is_git": False}
    before_entries = dict(before.get("entries") or {})
    after_entries = dict(after.get("entries") or {})
    before_digests = dict(before.get("digests") or {})
    after_digests = dict(after.get("digests") or {})
    changed = []
    for path, code in after_entries.items():
        if path not in before_entries or before_entries[path] != code:
            changed.append(path)
        elif before_digests.get(path) != after_digests.get(path):
            changed.append(path)
    reverted = [path for path in before_entries if path not in after_entries]
    return {
        "is_git": True,
        "branch_before": before.get("branch"),
        "branch_after": after.get("branch"),
        "head_before": before.get("head"),
        "head_after": after.get("head"),
        "branch_changed": before.get("branch") != after.get("branch"),
        "head_changed": before.get("head") != after.get("head"),
        "preexisting_dirty": sorted(before_entries),
        "task_changed_files": sorted(changed),
        "preexisting_changes_reverted": sorted(reverted),
        "dirty_after": len(after_entries),
    }


# ---------------------------------------------------------------------------
# Command guard
# ---------------------------------------------------------------------------

_GIT_INVOCATION_RE = re.compile(
    r"(?i)(?:^|[;&|(]\s*|\s|[\"'\\/])git(?:\.exe)?[\"']?"
    r"((?:\s+(?:-C|-c|--git-dir|--work-tree|--namespace)(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|\s+--?[A-Za-z][\w-]*(?:=\S+)?)*)"
    r"\s+([A-Za-z][\w-]*)([^;&|]*)"
)
_GIT_READ = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "blame", "describe",
    "shortlog", "cat-file", "merge-base", "rev-list", "ls-tree", "whatchanged", "fetch", "version", "help",
})
# Bounded read-only forms only: anything else (``stash drop``, ``worktree add``,
# ``stash show --output=...``) fails the full match and stays denied.
_GIT_STASH_READ_RE = re.compile(
    r"(?i)(?:list(?:\s+(?:--oneline|--stat|--name-only|--no-color|-n\s*\d+|--max-count=\d+))*"
    r"|show(?:\s+(?:--stat|--name-only|--name-status|--no-color|--oneline))*(?:\s+stash@\{\d+\})?)"
)
_GIT_WORKTREE_LIST_RE = re.compile(r"(?i)list(?:\s+(?:--porcelain|-v|--verbose|-z))*")
_ADB_RE = re.compile(r"(?i)(?:^|[\s;&|(\"'\\/])adb(?:\.exe)?(?=[\s\"']|$)")
_ADB_DENY_RE = re.compile(
    r"(?i)\badb(?:\.exe)?\b[^;&|]*\b(?:uninstall|reboot|root|unroot|remount|disable-verity|sideload|"
    r"shell\s+(?:[^;&|]*\s)?(?:rm|pm\s+clear|pm\s+uninstall|wipe|reboot|setprop|settings\s+put|svc))\b"
)
_ABS_WIN_PATH_RE = re.compile(r"(?i)(?<![\w/])([A-Z]:\\[^\s\"'<>|;&]*)")
_WRITE_VERB_RE = re.compile(
    r"(?i)(?:^|[;&|(]\s*|\s)(?:mkdir|new-item|copy-item|copy|cp|xcopy|robocopy|move-item|move|mv|"
    r"add-content|tee|tee-object|set-content|out-file|rename-item|install|expand-archive|tar)\b"
)
_ENV_DUMP_RE = re.compile(r"(?i)(?:^|[;&|]\s*)(?:printenv|env|set)\s*(?:$|[;&|])|get-childitem\s+env:|\bgci\s+env:|\bls\s+env:|\bdir\s+env:")
_GIT_ENV_OVERRIDE_RE = re.compile(
    r"(?i)(?:\bGIT_[A-Z0-9_]+\s*=|\$env:GIT_[A-Z0-9_]+\s*=|"
    r"(?:^|[;&|])\s*(?:export|set|set-item)\b[^;&|]*\bGIT_[A-Z0-9_]+\b)"
)


def _git_query_environment() -> dict[str, str]:
    return build_aoitalk_subprocess_env(extra_env={
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    })


def _git_query_base(workspace: Path) -> list[str]:
    return [
        "git", "-C", str(workspace),
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
    ]


def _git_filter_override_args(workspace: Path, env: Mapping[str, str]) -> Optional[list[str]]:
    def config_query(*args: str) -> Optional[subprocess.CompletedProcess[bytes]]:
        try:
            return subprocess.run(
                [*_git_query_base(workspace), "config", *args],
                capture_output=True,
                timeout=2,
                check=False,
                env=dict(env),
            )
        except (OSError, subprocess.SubprocessError):
            return None

    worktree_config = config_query("--local", "--get", "extensions.worktreeConfig")
    if worktree_config is None or worktree_config.returncode not in {0, 1}:
        return None
    scopes = ["--local"]
    if worktree_config.returncode == 0 and os.fsdecode(worktree_config.stdout).strip().lower() == "true":
        scopes.append("--worktree")
    filters: set[str] = set()
    for scope in scopes:
        result = config_query(
            scope,
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$",
        )
        if result is None or result.returncode not in {0, 1}:
            return None
        for raw_key in result.stdout.splitlines():
            key = os.fsdecode(raw_key)
            match = re.fullmatch(r"filter\.([A-Za-z0-9._-]+)\.(?:clean|smudge|process|required)", key, re.IGNORECASE)
            if match is None:
                return None
            filters.add(match.group(1))
    overrides: list[str] = []
    for name in sorted(filters):
        overrides.extend((
            "-c", f"filter.{name}.clean=",
            "-c", f"filter.{name}.smudge=",
            "-c", f"filter.{name}.process=",
            "-c", f"filter.{name}.required=false",
        ))
    return overrides


def _git_query(workspace: Path, *args: str) -> Optional[str]:
    """Run a bounded read-only Git query with sanitized env and inert hooks/filters."""

    env = _git_query_environment()
    filter_args = _git_filter_override_args(workspace, env)
    if filter_args is None:
        return None
    try:
        result = subprocess.run(
            [*_git_query_base(workspace), *filter_args, *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_query_bytes(workspace: Path, *args: str) -> Optional[bytes]:
    env = _git_query_environment()
    filter_args = _git_filter_override_args(workspace, env)
    if filter_args is None:
        return None
    try:
        result = subprocess.run(
            [*_git_query_base(workspace), *filter_args, *args],
            capture_output=True,
            timeout=2,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_index_paths(workspace: Path) -> Optional[set[str]]:
    raw = _git_query_bytes(workspace, "diff", "--cached", "--name-only", "--no-renames", "-z")
    if raw is None:
        return None
    return {os.fsdecode(path) for path in raw.split(b"\0") if path}


def _git_worktree_paths(workspace: Path) -> Optional[set[str]]:
    raw = _git_query_bytes(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if raw is None:
        return None
    entries = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) >= 4:
            paths.add(os.fsdecode(entry[3:]))
        status = entry[:2]
        if b"R" in status or b"C" in status:
            if index < len(entries) and entries[index]:
                paths.add(os.fsdecode(entries[index]))
                index += 1
    return paths


def _git_workspace_is_repository_root(workspace: Path) -> bool:
    root = _git_query(workspace, "rev-parse", "--show-toplevel")
    if not root:
        return False
    try:
        return os.path.normcase(os.path.realpath(root)) == os.path.normcase(os.path.realpath(workspace))
    except (OSError, ValueError):
        return False


def _git_add_paths_are_scoped(rest: str, workspace: Path) -> Optional[set[str]]:
    try:
        paths = shlex.split(rest, posix=True)
    except ValueError:
        return None
    if paths and paths[0] == "--":
        paths = paths[1:]
    if not paths:
        return None
    root = Path(os.path.realpath(workspace))
    result: set[str] = set()
    for value in paths:
        portable = value.replace("\\", "/")
        parts = portable.split("/")
        if (
            value.startswith("-")
            or portable.startswith("/")
            or re.match(r"^[A-Za-z]:", portable)
            or value == "."
            or ".." in parts
            or any(char in value for char in "*?[]")
            or value.startswith(":")
        ):
            return None
        try:
            resolved = Path(os.path.realpath(os.path.abspath(workspace / value)))
        except (OSError, ValueError):
            return None
        if not _path_within(resolved, root):
            return None
        if resolved.is_dir():
            return None
        if not resolved.is_file() and _git_query(workspace, "ls-files", "--error-unmatch", "--", portable) is None:
            return None
        result.add(Path(portable).as_posix())
    return result


def _git_commit_message_is_scoped(rest: str) -> bool:
    try:
        args = shlex.split(rest, posix=True)
    except ValueError:
        return False
    has_message = False
    index = 0
    while index < len(args):
        token = args[index]
        if (
            token in {"-m", "--message"}
            and index + 1 < len(args)
            and args[index + 1]
            and not args[index + 1].startswith("-")
        ):
            has_message = True
            index += 2
            continue
        if token.startswith("--message=") and token[len("--message="):] and not token[len("--message="):].startswith("-"):
            has_message = True
            index += 1
            continue
        return False
    return has_message


def _is_git_branch_ref(value: str) -> bool:
    if value == "HEAD":
        return True
    if value.startswith("refs/"):
        if not value.startswith("refs/heads/"):
            return False
        value = value[len("refs/heads/"):]
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value))


def _git_push_is_scoped(rest: str, workspace: Path) -> bool:
    try:
        args = shlex.split(rest, posix=True)
    except ValueError:
        return False
    if len(args) != 2 or any(arg.startswith(("-", "+")) for arg in args):
        return False
    remote, refspec = args
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}", remote):
        return False
    if not _git_workspace_is_repository_root(workspace):
        return False
    configured_remotes = _git_query(workspace, "remote")
    if configured_remotes is None or remote not in configured_remotes.splitlines():
        return False
    refs = refspec.split(":")
    if len(refs) not in {1, 2} or not all(_is_git_branch_ref(ref) for ref in refs):
        return False
    source = refs[0]
    destination = refs[-1]
    if source == "HEAD":
        if len(refs) != 2 or destination == "HEAD":
            return False
        return _git_query(workspace, "rev-parse", "--verify", "HEAD^{commit}") is not None
    branch_ref = source if source.startswith("refs/heads/") else f"refs/heads/{source}"
    if _git_query(workspace, "show-ref", "--verify", "--quiet", branch_ref) is None:
        return False
    if not source.startswith("refs/heads/"):
        if _git_query(workspace, "show-ref", "--verify", "--quiet", f"refs/tags/{source}") is not None:
            return False
    return destination != "HEAD"


def _git_write_command_details(command: str) -> Optional[tuple[str, str]]:
    text = str(command or "")
    if any(value in text for value in ("\n", "\r", "`", "$(", "${")):
        return None
    matches = list(_GIT_INVOCATION_RE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    sub = match.group(2).lower()
    if sub not in {"add", "commit", "push"} or (match.group(1) or "").strip():
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    if (
        len(tokens) < 2
        or tokens[0].casefold() not in {"git", "git.exe"}
        or tokens[1].casefold() != sub
        or any(token and all(char in ";&|()<>" for char in token) for token in tokens)
    ):
        return None
    return sub, (match.group(3) or "").strip()


def _posix_command_write_paths(command: str) -> tuple[bool, list[str]]:
    """Inspect literal POSIX paths; the scoped backend still owns isolation."""

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    paths: list[str] = []
    write_executable = False
    command_start = True
    for token in lexer:
        if not token.strip(";&|()"):
            command_start = True
            continue
        if command_start and _WRITE_VERB_RE.fullmatch(os.path.basename(token)):
            # /usr/bin/cp is a read of the executable, not a write target.
            write_executable = True
            command_start = False
            continue
        command_start = False
        if not token.startswith("/") and "://" in token:
            continue
        candidate = token if token.startswith("/") else token.partition("=")[2]
        if not candidate:
            short_option = re.fullmatch(r"-[A-Za-z]+(/.*)", token)
            candidate = short_option.group(1) if short_option else ""
        if candidate.startswith("/"):
            paths.append(candidate)
    return write_executable, paths


def check_command(
    command: str,
    *,
    spec: LocalAgentTaskSpec,
    workspace: Path,
) -> Optional[str]:
    """Return a denial reason, or ``None`` when the command text is allowed."""

    from ..tools.external_llm_permission import _command_looks_destructive

    text = str(command or "")
    if not text.strip():
        return "empty command"
    if len(text) > 8000:
        return "command is too long"
    if _command_looks_destructive(text):
        return ("destructive command blocked (delete / overwrite redirect / reset --hard / clean -f / "
                "force push etc.). Use the file tools for edits; never discard existing changes")
    if _SECRET_PATH_TOKEN_RE.search(text):
        return "commands may not read or reference secret files such as .env, private keys or credential stores"
    if _ENV_DUMP_RE.search(text):
        return "dumping the process environment is not allowed"
    git_matches = list(_GIT_INVOCATION_RE.finditer(text))
    git_write_matches = [
        match for match in git_matches
        if match.group(2).lower() in {"add", "commit", "push"}
    ]
    if git_write_matches:
        if _GIT_ENV_OVERRIDE_RE.search(text):
            return "Git writes may not override Git directory or config through environment variables"
        if _git_write_command_details(text) is None:
            return "Git writes must be one direct Git command without shell wrappers or command chaining"
    for match in git_matches:
        sub = match.group(2).lower()
        rest = (match.group(3) or "").strip()
        global_options = (match.group(1) or "").strip()
        if sub in _GIT_READ:
            continue
        if sub == "branch" and (not rest or re.fullmatch(r"(?:(?:-a|-r|-v|-vv|--list|--show-current|--all|--no-color)\s*)+", rest)):
            continue
        if sub == "remote" and (not rest or rest in {"-v", "show origin"}):
            continue
        if sub == "stash" and _GIT_STASH_READ_RE.fullmatch(rest):
            continue
        if sub == "worktree" and _GIT_WORKTREE_LIST_RE.fullmatch(rest):
            continue
        if sub == "tag" and (not rest or rest.startswith(("-l", "--list"))):
            continue
        if sub == "config" and re.match(r"(?:--get|--list|-l)\b", rest):
            continue
        if sub in {"add", "commit"} and spec.mode == "mutate" and spec.allow_commands and spec.allow_git_commit:
            if global_options:
                return "Git writes must use the task workspace repository without -C / --git-dir / --work-tree overrides"
            if not _git_workspace_is_repository_root(workspace):
                return "Git writes are limited to a repository rooted at the assigned workspace"
            if sub == "add" and _git_add_paths_are_scoped(rest, workspace) is None:
                return "git add must name explicit files inside the assigned workspace"
            if sub == "commit" and not _git_commit_message_is_scoped(rest):
                return "git commit requires only a normal -m/--message option; path and history options are blocked"
            continue
        if sub == "push" and spec.mode == "mutate" and spec.allow_commands and spec.allow_git_push:
            if global_options:
                return "Git writes must use the task workspace repository without -C / --git-dir / --work-tree overrides"
            if _git_push_is_scoped(rest, workspace):
                continue
            return "git push must use a configured remote and an existing ordinary branch refspec from the assigned repository"
        if sub == "apply" and spec.mode == "mutate" and "--reverse" not in rest and "-R" not in rest.split():
            continue
        return (f"git {sub} is not allowed in local agent tasks (no branch switching, stash, reset, clean, "
                "checkout, restore, rebase, merge, pull, push or history rewriting)")
    if _ADB_RE.search(text):
        if not spec.allow_adb:
            return "adb is not enabled for this task (allow_adb=false)"
        if _ADB_DENY_RE.search(text):
            return "destructive adb operation blocked (uninstall / reboot / remount / shell rm / pm clear etc.)"
    writes = _WRITE_VERB_RE.search(text) is not None
    paths = [match.group(1).rstrip(".,)") for match in _ABS_WIN_PATH_RE.finditer(text)]
    if os.name != "nt":
        try:
            write_executable, posix_paths = _posix_command_write_paths(text)
        except ValueError:
            if writes:
                return "file-writing command has invalid shell quoting"
        else:
            writes = writes or write_executable
            paths.extend(posix_paths)
    if writes:
        # Reading host tools outside the workspace (SDKs, adb) is fine; a
        # write verb combined with an outside absolute path is not.
        for candidate in paths:
            try:
                resolved = Path(os.path.realpath(os.path.abspath(candidate)))
            except (OSError, ValueError):
                return "commands may not write outside the task workspace"
            if not _path_within(resolved, workspace):
                return "commands may not write outside the task workspace"
    if spec.mode == "read_only" and writes:
        return "read_only task: file-writing commands are not allowed"
    return None


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------


class AoiTalkLocalLLMBackend:
    """Adapter over an existing OpenAI-compatible local client.

    Both the managed ``openai_compatible_local`` backend and Ollama expose a
    local OpenAI-shaped completion boundary.  The task runner owns the tool
    loop; it must never call the provider's higher-level chat/tool loop here.
    """

    def __init__(
        self,
        config: Any,
        *,
        model: str | None = None,
        provider: str = "openai_compatible_local",
        effort: str | None = None,
        strict_route: bool = False,
        user_id: str | None = None,
        project_id: str | None = None,
    ):
        self.config = config
        self.strict_route = strict_route
        self.user_id = str(user_id) if user_id is not None else None
        self.project_id = str(project_id) if project_id is not None else None
        self._client = None
        self.provider = str(provider or "openai_compatible_local").strip().lower()
        if self.provider not in {"openai_compatible_local", "ollama"}:
            raise ValueError("local task backend requires a local provider")
        self.model = model or self.resolve_model(config)
        self.effort = str(effort or "").strip()

    @staticmethod
    def resolve_model(config: Any) -> str:
        return str(
            _cfg(config, "local_agent_tasks.model", "")
            or _cfg(config, "openai_compatible_local.model", "")
            or os.getenv("OPENAI_COMPATIBLE_LOCAL_MODEL", "")
            or "local-model"
        )

    def _get_client(self):
        if self._client is None:
            from ..llm.manager import create_llm_client_for_target

            self._client = create_llm_client_for_target(
                self.config,
                provider=self.provider,
                model=self.model,
                effort=self.effort,
                provider_options={"lightweight_client": True, "ephemeral_session_client": True,
                                  "enable_tools": False},
            )
            if self.provider == "ollama" and self.effort:
                set_mode = getattr(self._client, "set_llm_mode", None)
                if callable(set_mode):
                    mode = self.effort
                    try:
                        from ..llm.provider_mode_adapters import ollama_mode_options_for_model

                        options = ollama_mode_options_for_model(self.model)
                        if mode not in options and "thinking" in options:
                            mode = "thinking"
                        elif mode not in options and options:
                            mode = options[0]
                    except Exception:
                        pass
                    set_mode(mode)
        if self.strict_route and getattr(self._client, "model_name", None) != self.model:
            raise RuntimeError("local_task_resolved_model_mismatch")
        if self.user_id is not None:
            # Reapply the durable task owner before every access to the cached
            # target client, including completion/usage after request building.
            set_context = getattr(self._client, "set_session_context", None)
            if not callable(set_context):
                raise RuntimeError("local_task_client_owner_unsupported")
            from .turn_context import get_turn_context
            context = get_turn_context()
            if context.agent_id and context.session_id:
                if context.user_id != self.user_id:
                    raise RuntimeError("local_task_client_owner_mismatch")
                self._client.current_session_id = context.session_id
                set_context(user_id=self.user_id, metadata={"session_id": context.session_id})
            else:
                self._client.current_session_id = None
                set_context(user_id=self.user_id, metadata={"session_id": ""})
        # Project attribution is a trusted coordinator value.  Set it on
        # every access, including an explicit None, so a cached client cannot
        # inherit a parent task's selected project.
        setattr(self._client, "current_project_id", self.project_id)
        return self._client

    def describe(self) -> dict[str, Any]:
        client = self._get_client()
        return {"provider": self.provider, "model": getattr(client, "model_name", self.model),
                "base_url": getattr(client, "base_url", None), "effort": self.effort}

    def build_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        client = self._get_client()
        request = client._build_api_kwargs(messages, 0.2, None, tools_enabled=False)
        if self.strict_route and request.get("model") != self.model:
            raise RuntimeError("local_task_wire_model_mismatch")
        request["messages"] = messages
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        request.pop("response_format", None)
        return request

    def complete(self, request: dict[str, Any]) -> Any:
        if self.strict_route and request.get("model") != self.model:
            raise RuntimeError("local_task_wire_model_mismatch")
        client = self._get_client()
        completion = getattr(client, "_tool_loop_completion", None)
        if callable(completion):
            return completion(request)
        # Ollama's public ``generate`` method owns a different tool loop.  Use
        # its one-completion primitive so the common task loop remains the
        # only tool router and ToolGuard remains authoritative.
        completion = getattr(client, "_create_completion_with_tool_fallback", None)
        if callable(completion):
            return completion(request, request_type="local_agent_task")
        raise RuntimeError("local provider has no one-completion API")

    def message_content(self, message: Any) -> str:
        client = self._get_client()
        getter = getattr(client, "_message_content", None)
        if callable(getter):
            return getter(message)
        if isinstance(message, Mapping):
            return str(message.get("content") or "")
        return str(getattr(message, "content", "") or "")

    @property
    def restore_tool_arguments(self):
        gateway = getattr(self._get_client(), "_privacy_gateway", None)
        return getattr(gateway, "restore_tool_arguments", None)

    def close(self) -> None:
        client, self._client = self._client, None
        cleanup = getattr(client, "cleanup", None)
        if callable(cleanup):
            try:
                import asyncio

                result = cleanup()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            except Exception:  # pragma: no cover - best effort
                logger.debug("local agent LLM client cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def resolve_task_route(config: Any, spec: LocalAgentTaskSpec) -> dict[str, str]:
    """Resolve only existing provider/catalog routes without starting a model."""
    if spec.provider is None and spec.model is None and spec.effort is None:
        return {"provider": "openai_compatible_local",
                "model": AoiTalkLocalLLMBackend.resolve_model(config), "effort": ""}
    provider, model, effort = spec.provider, str(spec.model or ""), spec.effort
    if not model:
        raise LocalAgentTaskError("invalid_task_route", "an explicit catalog model is required")
    if provider == "codex-cli":
        # Keep a narrow preflight allowlist for the Operations native route.
        # CodexTaskCompletionClient still performs the authoritative app-server
        # ``model/list`` check immediately before the model turn; this gate only
        # prevents stale/unknown pinned routes from reaching that client.
        codex_route_efforts = {
            "gpt-5.6-sol": frozenset({"high"}),
            "gpt-6.1-sol": frozenset({"high"}),
        }
        allowed_efforts = codex_route_efforts.get(model)
        if allowed_efforts is None:
            raise LocalAgentTaskError("invalid_task_route", "unsupported Codex task model or effort")
        # The static route policy must agree with the current model catalog's
        # effort options.  If the catalog no longer exposes the pinned effort,
        # fail closed instead of substituting another model or effort.
        from .llm_model_catalog import reasoning_effort_options_for_model

        catalog_efforts = frozenset(
            reasoning_effort_options_for_model("codex-cli", model)
        )
        allowed_efforts = allowed_efforts & catalog_efforts
        selected_effort = effort or "high"
        if selected_effort not in allowed_efforts:
            raise LocalAgentTaskError("invalid_task_route", "unsupported Codex task model or effort")
        return {"provider": provider, "model": model, "effort": selected_effort}
    if provider == "openai_compatible_local":
        from ..llm.openai_compatible_local_profiles import (
            strata_model_profile, llama_cpp_model_profile,
            llama_cpp_reasoning_effort_metadata,
            llama_cpp_thinking_mode,
        )
        strata = strata_model_profile(model)
        profile = strata or llama_cpp_model_profile(model)
        if profile:
            model = str(profile["id"])
        elif model != AoiTalkLocalLLMBackend.resolve_model(config):
            raise LocalAgentTaskError("invalid_task_route", "model is absent from the existing local catalog")
        capabilities = profile.get("capabilities", {}) if profile else {}
        if capabilities.get("tools") is False:
            raise LocalAgentTaskError("invalid_task_route", "model catalog does not support task tools")
        metadata = llama_cpp_reasoning_effort_metadata(model) if not strata else None
        thinking_mode = llama_cpp_thinking_mode(model) if not strata else None
        if thinking_mode == "always":
            # LFM2.5's official template has no supported non-thinking branch.
            # Keep task routes aligned with the catalog instead of borrowing
            # the generic local-model fast/thinking options.
            options = ["always"]
            default = "always"
            if effort in {"fast", "thinking"}:
                # Accept stale route values during a model switch, but never
                # carry their non-thinking meaning to the provider.
                effort = "always"
            elif effort == "":
                effort = None
        else:
            generic_options = ["fast"] if capabilities.get("reasoning") is False else ["fast", "thinking"]
            options = metadata["options"] if metadata else generic_options
            default = metadata["default"] if metadata else ("thinking" if strata else "")
        if effort is not None and effort not in options:
            raise LocalAgentTaskError("invalid_task_route", "unsupported local reasoning effort")
        return {"provider": provider, "model": model, "effort": default if effort is None else effort}
    if provider == "ollama":
        from ..llm.provider_mode_adapters import ollama_mode_options_for_model
        if model != str(_cfg(config, "ollama.model", "")):
            raise LocalAgentTaskError("invalid_task_route", "model is not the configured Ollama model")
        options = ollama_mode_options_for_model(model)
        if effort is not None and effort not in options:
            raise LocalAgentTaskError("invalid_task_route", "unsupported Ollama reasoning effort")
        return {"provider": provider, "model": model, "effort": effort or ""}
    raise LocalAgentTaskError("invalid_task_route", "unsupported task provider")


@dataclass
class TaskRunContext:
    """Mutable execution state shared by the loop, the tool guard and the manager."""

    task_id: str
    spec: LocalAgentTaskSpec
    limits: LocalAgentTaskLimits
    cancel_event: threading.Event
    emit: Callable[[str, Mapping[str, Any]], None]
    update: Callable[[Mapping[str, Any]], None]
    redactor: SecretRedactor
    deadline: float = 0.0
    counters: dict[str, int] = field(default_factory=lambda: {
        "llm_calls": 0, "tool_calls": 0, "tool_failures": 0, "commands": 0,
        "commands_failed": 0, "blocked_calls": 0, "files_written": 0,
    })
    stop_reason: str | None = None
    touched_files: set[str] = field(default_factory=set)
    created_files: set[str] = field(default_factory=set)
    background_jobs: set[str] = field(default_factory=set)
    command_log: list[dict[str, Any]] = field(default_factory=list)
    _recent: list[str] = field(default_factory=list)
    _repeat_blocks: int = 0
    _failure_streak: int = 0
    user_id: str | None = None
    project_id: str | None = None
    instruction_review: ReviewedHostInstructions | None = None
    codex_profile: Any | None = None
    codex_profile_receipt: Mapping[str, Any] | None = None
    provider_usage_recorder: Callable[[Mapping[str, Any]], None] | None = None
    # Internal-only additions, never accepted from model/request JSON.
    extra_tools: tuple[Any, ...] = ()
    agent_system_prompt: str = ""
    execution_checkpoint: Callable[[], None] | None = None
    verified_tool_results: int = 0
    unfinished_background_jobs: int = 0
    # Wall-clock bookkeeping for the response contract.  A cold local model
    # is loaded inside the first request, so that response time is reported
    # on its own: a timeout can then be told apart from slow generation.
    started_monotonic: float = 0.0
    first_llm_response_seconds: float | None = None

    def timing(self) -> dict[str, Any]:
        elapsed = round(time.monotonic() - self.started_monotonic, 1) if self.started_monotonic else None
        return {
            "timeout_sec": self.spec.timeout_sec,
            "elapsed_sec": elapsed,
            "first_llm_response_sec": self.first_llm_response_seconds,
            "llm_calls": self.counters.get("llm_calls", 0),
        }

    def check_running(self) -> None:
        if self.cancel_event.is_set():
            raise _TaskStop("cancelled")
        if self.execution_checkpoint is not None:
            try:
                self.execution_checkpoint()
            except Exception as exc:
                raise _TaskStop("timeout" if getattr(exc, "code", None) == "task_timeout" else "authority_revoked") from None
        if self.deadline and time.monotonic() > self.deadline:
            raise _TaskStop("timeout")
        if self.stop_reason:
            raise _TaskStop(self.stop_reason)
        if self.codex_profile is not None:
            from .codex_app_profiles import CodexProfileError
            try:
                self.codex_profile.check(owner=self.user_id, read_roots=[Path(self.spec.workspace)])
            except CodexProfileError:
                self.stop_reason = "codex_profile_invalidated"
                raise _TaskStop(self.stop_reason) from None
        if self.instruction_review is not None:
            from ..llm.codex_task_completion import CodexTaskProtocolError
            try:
                self.instruction_review.check()
            except CodexTaskProtocolError:
                self.stop_reason = "instruction_review_invalidated"
                raise _TaskStop(self.stop_reason) from None

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic()) if self.deadline else float("inf")


_READ_TOOLS = ("read_file", "list_directory", "search_files")
_MUTATION_TOOLS = ("create_file", "edit_file", "insert_to_file", "append_to_file", "undo_edit", "delete_file")
_COMMAND_TOOLS = ("execute_command", "read_command_output", "write_command_input", "stop_command")
_POLL_TOOLS = frozenset({"read_command_output"})


class ToolGuard:
    """Wrap existing os_operations tools with task-scoped limits and auditing."""

    _SEARCH_CONTENT_KEYS = frozenset(
        {
            "content",
            "contents",
            "data",
            "line",
            "lines",
            "match",
            "matches",
            "preview",
            "snippet",
            "snippets",
            "text",
            "value",
        }
    )

    def __init__(self, ctx: TaskRunContext, scope: Any):
        self.ctx = ctx
        self.scope = scope
        self.workspace = Path(ctx.spec.workspace)
        self.scoped_commands = False
        self._command_serial = 0
        self._background_call_ids: dict[str, str] = {}

    # -- helpers ---------------------------------------------------------
    def _deny(self, tool: str, reason: str) -> dict[str, Any]:
        self.ctx.counters["blocked_calls"] += 1
        self.ctx.emit("tool_blocked", {"tool": tool, "reason": reason})
        return {"success": False, "error": f"Blocked by local task policy: {reason}"}

    def _signature(self, tool: str, kwargs: Mapping[str, Any]) -> str:
        return tool + ":" + json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)

    def _repeat_check(self, tool: str, kwargs: Mapping[str, Any]) -> Optional[str]:
        if tool in _POLL_TOOLS:
            return None
        signature = self._signature(tool, kwargs)
        recent = self.ctx._recent
        recent.append(signature)
        del recent[:-8]
        if recent.count(signature) >= self.ctx.limits.repeat_limit:
            self.ctx._repeat_blocks += 1
            if self.ctx._repeat_blocks >= 2:
                self.ctx.stop_reason = "repeated_action"
            return ("the identical call was already made repeatedly without progress; change approach, "
                    "or finish with the final report")
        return None

    def _policy_relative_path(self, path: Path) -> str:
        """Return a path relative to the scope root used for policy checks.

        Local tasks normally expose only ``workspace``.  Trusted harness
        scopes may add explicit read roots for external, read-only grants;
        those roots remain usable while their contents still receive the same
        relative-path policy.  Omitting the root itself from the relative
        value also avoids treating a caller-selected grant directory named
        ``credentials`` as a credential namespace by itself.
        """

        roots: list[Path] = [self.workspace]
        for name in ("read_roots", "write_roots"):
            for root in getattr(self.scope, name, ()) or ():
                try:
                    candidate = Path(root)
                except (TypeError, ValueError):
                    continue
                if candidate not in roots:
                    roots.append(candidate)
        for root in roots:
            try:
                return str(path.relative_to(root))
            except ValueError:
                continue
        # The scope has already authorized this canonical path.  A basename
        # fallback preserves explicitly granted roots for scope implementations
        # that do not expose their root collection to the guard.
        return path.name

    @staticmethod
    def _path_parts(path: str) -> tuple[str, ...]:
        return tuple(part for part in re.split(r"[\\/]", str(path or "")) if part)

    def _path_policy_error(self, resolved: Path) -> Optional[str]:
        relative = self._policy_relative_path(resolved)
        if is_sensitive_relative_path(relative):
            return "secret files and credential namespaces are not accessible"
        if any(part.casefold() == ".git" for part in self._path_parts(relative)):
            return "the .git directory is not accessible"

        try:
            info = resolved.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return "the file could not be inspected safely"
        if stat.S_ISREG(info.st_mode) and int(getattr(info, "st_nlink", 1)) > 1:
            return "hardlink aliases are not accessible"
        return None

    def _resolve(self, path: Any, *, write: bool) -> tuple[Optional[Path], Optional[str]]:
        raw = str(path or "")
        if not raw:
            return None, "path is required"
        try:
            resolved = (self.scope.assert_mutation_allowed(raw, "write") if write
                        else self.scope.assert_read_allowed(raw))
        except Exception as exc:  # RunScopeViolation / RunScopeError
            return None, f"path is outside the task workspace: {type(exc).__name__}"
        resolved = Path(resolved)
        policy_error = self._path_policy_error(resolved)
        if policy_error:
            return None, policy_error
        return resolved, None

    def _record(self, tool: str, result: Any) -> Any:
        success = not (isinstance(result, Mapping) and (result.get("success") is False
            or result.get("ok") is False or result.get("isError") is True))
        if success:
            command_finished = (isinstance(result, Mapping)
                and result.get("return_code", result.get("exit_code")) == 0
                and result.get("status") != "running")
            if tool not in _COMMAND_TOOLS or command_finished:
                self.ctx.verified_tool_results += 1
            self.ctx._failure_streak = 0
        else:
            self.ctx.counters["tool_failures"] += 1
            self.ctx._failure_streak += 1
            if self.ctx._failure_streak >= self.ctx.limits.failure_streak_limit:
                self.ctx.stop_reason = "no_progress"
        return self.ctx.redactor.value(result, self.ctx.limits.tool_result_chars)

    # -- wrapping --------------------------------------------------------
    def wrap(self, tool_def: Any) -> Any:
        name = tool_def.name
        original = tool_def.function

        @functools.wraps(original)
        def guarded(**kwargs):
            if self.ctx.cancel_event.is_set():
                return {"success": False, "error": "task was cancelled"}
            self.ctx.check_running()
            self.ctx.counters["tool_calls"] += 1
            repeat = self._repeat_check(name, kwargs)
            if repeat:
                return self._deny(name, repeat)
            handler = getattr(self, f"_call_{name}", None)
            if handler is None:
                result = original(**kwargs)
            else:
                result = handler(original, kwargs)
            return self._record(name, result)

        return dataclasses.replace(tool_def, function=guarded)

    def _read_call(self, original: Callable, kwargs: dict[str, Any], key: str = "path") -> Any:
        target = kwargs.get(key) or "."
        resolved, error = self._resolve(target, write=False)
        if error:
            return self._deny(original.__name__, error)
        kwargs = dict(kwargs)
        kwargs[key] = str(resolved)
        self.ctx.check_running()
        result = original(**kwargs)
        self.ctx.check_running()
        return result

    def _call_read_file(self, original, kwargs):
        return self._read_call(original, kwargs)

    def _call_list_directory(self, original, kwargs):
        return self._read_call(original, kwargs)

    @staticmethod
    def _search_item_path(item: Any) -> Optional[str]:
        if isinstance(item, Mapping):
            for key in ("path", "file", "filename", "name"):
                value = item.get(key)
                if isinstance(value, (str, os.PathLike)) and str(value).strip():
                    return str(value).strip()
            return None
        if isinstance(item, (str, os.PathLike)):
            value = str(item).strip()
            if value.startswith("📄"):
                value = value[1:].strip()
            # file_system.search_files appends line locations to content
            # results, while name results may omit them.
            value = re.sub(r"\s+\(L\d+(?:,\s*L\d+)*\)\s*$", "", value)
            return value.strip() or None
        return None

    def _resolve_search_item(self, raw: str, root: Path) -> Optional[Path]:
        value = str(raw or "").strip()
        if not value:
            return None
        candidates: list[Path] = []
        try:
            path = Path(value)
        except (TypeError, ValueError):
            return None
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.append(root / path)
            # A few legacy search providers return workspace-relative paths
            # even when the search root is a nested directory.
            candidates.append(self.workspace / path)
        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(os.path.normpath(str(candidate)))
            if key in seen:
                continue
            seen.add(key)
            try:
                resolved = Path(self.scope.assert_read_allowed(str(candidate)))
            except Exception:
                continue
            if not resolved.exists() or not _path_within(resolved, root):
                continue
            if self._path_policy_error(resolved):
                continue
            return resolved
        return None

    def _filter_search_items(self, items: list[Any], root: Path) -> list[Any]:
        filtered: list[Any] = []
        for item in items:
            raw = self._search_item_path(item)
            if raw is not None and self._resolve_search_item(raw, root) is not None:
                if isinstance(item, Mapping):
                    filtered.append(
                        {
                            key: value
                            for key, value in item.items()
                            if str(key).casefold() not in self._SEARCH_CONTENT_KEYS
                        }
                    )
                else:
                    filtered.append(item)
        return filtered

    def _filter_search_text(self, value: str, root: Path) -> str:
        """Keep only the known file-system search metadata lines.

        ``search_files(search_content=True)`` currently returns a formatted
        string containing paths and line numbers, rather than snippets.  Keep
        that contract while dropping any unrecognised line so a future search
        implementation cannot accidentally pass file contents through this
        boundary without an explicit schema review.
        """

        kept: list[str] = []
        for line in str(value).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("Search results for ") or stripped.startswith(
                "No files found matching "
            ):
                kept.append(line)
                continue
            if stripped.startswith("... (showing first "):
                kept.append(line)
                continue
            raw = self._search_item_path(stripped)
            if raw is not None and self._resolve_search_item(raw, root) is not None:
                kept.append(line)
        return "\n".join(kept)

    def _call_search_files(self, original, kwargs):
        result = self._read_call(original, kwargs)
        # Drop matches coming from secret files, .git, out-of-scope paths, or
        # hardlink aliases before the model sees them.  The two existing
        # os_operations implementations use different result shapes, so both
        # list-based results and formatted content-search text are handled.
        requested_root = kwargs.get("path") or "."
        resolved_root, _root_error = self._resolve(requested_root, write=False)
        root = resolved_root or self.workspace
        if isinstance(result, Mapping):
            result = dict(result)
            for list_key in ("results", "matches", "files"):
                items = result.get(list_key)
                if isinstance(items, list):
                    result[list_key] = self._filter_search_items(items, root)
            content = result.get("content")
            if isinstance(content, str):
                result["content"] = self._filter_search_text(content, root)
            elif isinstance(content, list):
                result["content"] = self._filter_search_items(content, root)
            elif isinstance(content, Mapping):
                result["content"] = {
                    key: value
                    for key, value in content.items()
                    if str(key).casefold() not in self._SEARCH_CONTENT_KEYS
                }
        elif isinstance(result, str):
            result = self._filter_search_text(result, root)
        return result

    def _mutation_call(self, original, kwargs, operation: str = "write"):
        resolved, error = self._resolve(kwargs.get("path"), write=True)
        if error:
            return self._deny(original.__name__, error)
        kwargs = dict(kwargs)
        kwargs["path"] = str(resolved)
        existed = resolved.exists()
        result = original(**kwargs)
        if not (isinstance(result, Mapping) and result.get("success") is False):
            self.ctx.counters["files_written"] += 1
            self.ctx.touched_files.add(str(resolved))
            if not existed and operation == "create":
                self.ctx.created_files.add(os.path.normcase(str(resolved)))
            self.ctx.emit("file_changed", {"path": self._rel(resolved), "operation": operation})
        return result

    def _rel(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.workspace))
        except ValueError:
            return str(path)

    def _call_create_file(self, original, kwargs):
        return self._mutation_call(original, kwargs, "create")

    def _call_edit_file(self, original, kwargs):
        return self._mutation_call(original, kwargs, "edit")

    def _call_insert_to_file(self, original, kwargs):
        return self._mutation_call(original, kwargs, "insert")

    def _call_append_to_file(self, original, kwargs):
        return self._mutation_call(original, kwargs, "append")

    def _call_undo_edit(self, original, kwargs):
        return self._mutation_call(original, kwargs, "undo")

    def _call_delete_file(self, original, kwargs):
        resolved, error = self._resolve(kwargs.get("path"), write=True)
        if error:
            return self._deny("delete_file", error)
        if os.path.normcase(str(resolved)) not in self.ctx.created_files:
            return self._deny("delete_file", "only files created by this task may be deleted")
        return self._mutation_call(original, kwargs, "delete")

    # -- commands --------------------------------------------------------
    def _call_execute_command(self, original, kwargs):
        command = str(kwargs.get("command") or "")
        cwd_raw = kwargs.get("working_directory") or "."
        decision = self.scope.check_command_cwd(cwd_raw)
        if not decision.allowed:
            if decision.reason == "command cwd is not a directory":
                return self._deny("execute_command", "working_directory_not_directory")
            return self._deny("execute_command", "working_directory must be inside the task workspace")
        try:
            if not decision.path.exists():
                return self._deny("execute_command", "working_directory_not_found")
            if not decision.path.is_dir():
                return self._deny("execute_command", "working_directory_not_directory")
        except OSError:
            return self._deny("execute_command", "working_directory_unavailable")
        reason = check_command(command, spec=self.ctx.spec, workspace=self.workspace)
        if reason:
            return self._deny("execute_command", reason)
        background = bool(kwargs.get("run_in_background"))
        if background and len(self.ctx.background_jobs) >= self.ctx.limits.max_background_jobs:
            return self._deny("execute_command", "too many background commands for this task")
        self.ctx.counters["commands"] += 1
        shell = kwargs.get("shell")
        timeout = self.ctx.spec.command_timeout_sec
        requested = kwargs.get("timeout")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool) and requested > 0:
            timeout = min(timeout, int(requested))
        timeout = int(max(1, min(timeout, self.ctx.remaining())))
        started = time.monotonic()
        self._command_serial += 1
        try:
            from .turn_context import get_turn_context

            tool_call_id = str(get_turn_context().tool_call_id or "").strip()
        except Exception:
            tool_call_id = ""
        tool_call_id = tool_call_id or f"task-command-{self._command_serial}"
        self.ctx.emit("command_start", {"command": command, "cwd": self._rel(decision.path),
                                        "background": background, "timeout_sec": timeout,
                                        "tool_call_id": tool_call_id})
        if self.scoped_commands:
            return self._call_scoped_execute_command(
                command,
                decision.path,
                shell=shell,
                background=background,
                timeout=timeout,
                started=started,
                tool_call_id=tool_call_id,
            )
        # Ordinary local tasks retain their historical host-command contract.
        # Manual local tasks retain their historical host-command contract.
        # A trusted HarnessExecutionScope never reaches this branch: scoped
        # tasks use _call_scoped_execute_command below, independent of the
        # Personal/Enterprise feature profile and the os_operations wrapper.
        from ..security.agent_run_scope import bind_run_scope, reset_run_scope

        token = bind_run_scope(self.scope if self.scoped_commands else None)
        try:
            launched = original(command=command, working_directory=str(decision.path), shell=shell,
                                run_in_background=True)
            if not (isinstance(launched, Mapping) and launched.get("success") and launched.get("job_id")):
                if background:
                    return launched
                # Background execution disabled: bounded foreground fallback.
                result = original(command=command, working_directory=str(decision.path), shell=shell,
                                  timeout=timeout, run_in_background=False)
                return self._finish_command(command, result, started, tool_call_id=tool_call_id)
            job_id = str(launched["job_id"])
            self.ctx.background_jobs.add(job_id)
            self._background_call_ids[job_id] = tool_call_id
            if background:
                self.ctx.emit("command_background", {"job_id": job_id, "tool_call_id": tool_call_id})
                return launched
            return self._wait_foreground(command, job_id, timeout, started, tool_call_id=tool_call_id)
        finally:
            reset_run_scope(token)

    def _call_scoped_execute_command(
        self,
        command: str,
        cwd: Path,
        *,
        shell: str | None,
        background: bool,
        timeout: int,
        started: float,
        tool_call_id: str,
    ) -> dict[str, Any]:
        """Run through the concrete WSL/bwrap backend for a trusted scope.

        The ordinary ``os_operations.execute_command`` wrapper has a legacy
        host executor lane in Personal profile.  Calling it from a worker
        would make a valid HarnessExecutionScope depend on profile selection.
        The task guard therefore selects the verified backend first and then
        uses the scoped background registry directly.  ``BackgroundJobRegistry``
        captures the same immutable scope owner and calls ``WslBwrapBackend``
        for every process; an unavailable or unready backend is a denial.
        """

        from ..agent_harness.runner import _scoped_backend
        from ..security.harness_execution_scope import (
            HarnessExecutionScope,
            get_current_harness_execution_scope,
        )
        from ..tools.os_operations.background_jobs import (
            BackgroundJobError,
            get_background_job_registry,
        )
        from ..security.agent_run_scope import bind_run_scope, reset_run_scope

        upper = get_current_harness_execution_scope()
        if not isinstance(upper, HarnessExecutionScope):
            return self._finish_command(
                command,
                self._deny("execute_command", "trusted HarnessExecutionScope is required"),
                started,
                tool_call_id=tool_call_id,
            )
        prepared = _scoped_backend(self.scope, cwd)
        if isinstance(prepared, str):
            return self._finish_command(
                command,
                self._deny("execute_command", prepared),
                started,
                tool_call_id=tool_call_id,
            )
        registry = get_background_job_registry()
        token = bind_run_scope(self.scope)
        try:
            try:
                job_id = registry.start(command, cwd=str(cwd), shell=shell)
            except BackgroundJobError as exc:
                return self._finish_command(
                    command,
                    {"success": False, "error": str(exc)},
                    started,
                    tool_call_id=tool_call_id,
                )
            self.ctx.background_jobs.add(str(job_id))
            self._background_call_ids[str(job_id)] = tool_call_id
            if background:
                result = {"success": True, "job_id": str(job_id)}
                self.ctx.emit("command_background", {"job_id": str(job_id), "tool_call_id": tool_call_id})
                return result
            return self._wait_foreground(
                command,
                str(job_id),
                timeout,
                started,
                scope=self.scope,
                tool_call_id=tool_call_id,
            )
        finally:
            reset_run_scope(token)

    def _wait_foreground(
        self,
        command: str,
        job_id: str,
        timeout: int,
        started: float,
        *,
        scope: Any | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        from ..tools.os_operations.background_jobs import BackgroundJobError, get_background_job_registry

        registry = get_background_job_registry()
        deadline = started + timeout
        last_heartbeat = started
        stop_reason = None
        while True:
            try:
                info = registry.read(
                    job_id,
                    max_output_bytes=self.ctx.limits.tool_result_chars,
                    scope=scope,
                )
            except BackgroundJobError as exc:
                return {"success": False, "error": str(exc)}
            if info.get("status") != "running":
                break
            now = time.monotonic()
            if self.ctx.cancel_event.is_set():
                stop_reason = "cancelled"
            elif now >= deadline:
                stop_reason = "timeout"
            if stop_reason:
                with contextlib.suppress(Exception):
                    registry.stop(job_id, scope=scope)
                with contextlib.suppress(Exception):
                    info = registry.read(
                        job_id,
                        max_output_bytes=self.ctx.limits.tool_result_chars,
                        scope=scope,
                    )
                break
            if now - last_heartbeat >= 15:
                last_heartbeat = now
                self.ctx.update({"latest_activity": f"command running {int(now - started)}s"})
            time.sleep(0.25)
        self.ctx.background_jobs.discard(job_id)
        exit_code = info.get("exit_code")
        result = {
            "success": stop_reason is None and exit_code == 0,
            "output": info.get("output", ""),
            # Preserve the bounded stream fields from the owned background
            # registry.  ``output`` is a presentation aggregate; the native
            # model/telemetry contracts need stdout and stderr separately so
            # a failed test remains diagnosable without reconstructing it
            # from a Python repr or dropping the original stream.
            "stdout": info.get("stdout", ""),
            "stderr": info.get("stderr", ""),
            "return_code": exit_code,
            "status": info.get("status"),
            "finalization_error": info.get("finalization_error"),
            "finalization_published": info.get("finalization_published"),
            "timed_out": stop_reason == "timeout",
            "cancelled": stop_reason == "cancelled",
            "duration_seconds": round(time.monotonic() - started, 3),
            "truncated": bool(info.get("truncated")),
        }
        finalization_error = info.get("finalization_error")
        if finalization_error:
            result["success"] = False
            result["error_code"] = "command_finalization_failed"
            result["error"] = "Command finalization failed"
        if stop_reason == "timeout":
            result["error"] = f"Command timed out after {timeout} seconds and was stopped"
        elif stop_reason == "cancelled":
            result["error"] = "Command stopped because the task was cancelled"
        elif exit_code != 0:
            result["error"] = f"Command exited with code {exit_code}"
        return self._finish_command(command, result, started, tool_call_id=tool_call_id, job_id=job_id)

    def _finish_command(
        self,
        command: str,
        result: Any,
        started: float,
        *,
        tool_call_id: str | None = None,
        job_id: str | None = None,
    ) -> Any:
        success = isinstance(result, Mapping) and result.get("success") is True
        if not success:
            self.ctx.counters["commands_failed"] += 1
        entry = {
            "command": self.ctx.redactor.text(command, 300),
            "exit_code": result.get("return_code") if isinstance(result, Mapping) else None,
            "success": success,
            "timed_out": bool(isinstance(result, Mapping) and result.get("timed_out")),
            "duration_sec": round(time.monotonic() - started, 2),
            "tool_call_id": str(tool_call_id or ""),
        }
        if isinstance(result, Mapping):
            for key in ("finalization_published", "finalization_error"):
                if key in result:
                    entry[key] = self.ctx.redactor.value(result[key], 500)
            # Prefer the concrete streams.  ``output`` is a presentation
            # aggregate and may contain stderr; using it first would erase
            # the stdout/stderr distinction needed by native diagnostics.
            for source_key, target_key in (("stdout", "stdout"), ("stderr", "stderr"), ("output", "stdout")):
                value = result.get(source_key)
                if isinstance(value, str) and value and target_key not in entry:
                    entry[target_key] = self.ctx.redactor.text(value, 4_000)
        if job_id:
            entry["job_id"] = str(job_id)
        self.ctx.command_log.append(entry)
        del self.ctx.command_log[:-200]
        self.ctx.emit("command_end", entry)
        return result

    def _owned_job(self, tool: str, original, kwargs):
        job_id = str(kwargs.get("job_id") or "")
        if job_id not in self.ctx.background_jobs:
            return self._deny(tool, "job_id was not started by this task")
        if self.scoped_commands:
            from ..tools.os_operations.background_jobs import BackgroundJobError, get_background_job_registry

            registry = get_background_job_registry()
            try:
                if tool == "read_command_output":
                    result = registry.read(
                        job_id,
                        max_output_bytes=int(kwargs.get("max_output_bytes") or self.ctx.limits.tool_result_chars),
                        since_offset=int(kwargs.get("since_offset") or 0),
                        scope=self.scope,
                    )
                    result = self._normalize_background_read_result(result)
                elif tool == "write_command_input":
                    result = registry.write_stdin(job_id, str(kwargs.get("text") or ""), scope=self.scope)
                    result["success"] = True
                else:
                    result = registry.stop(job_id, scope=self.scope)
                    if isinstance(result, Mapping) and result.get("finalization_error"):
                        result = dict(result)
                        result["success"] = False
                        result["error_code"] = "command_finalization_failed"
                        result["error"] = "Command finalization failed"
                    else:
                        result["success"] = True
            except (BackgroundJobError, ValueError, TypeError) as exc:
                return {"success": False, "error": str(exc)}
            if tool == "stop_command":
                self.ctx.background_jobs.discard(job_id)
            if tool == "stop_command" or (
                tool == "read_command_output" and isinstance(result, Mapping) and result.get("status") == "exited"
            ):
                original_call_id = self._background_call_ids.pop(job_id, None)
                if original_call_id:
                    command_success = (
                        result.get("exit_code") == 0
                        and not result.get("finalization_error")
                    )
                    if not command_success:
                        self.ctx.counters["commands_failed"] += 1
                    self.ctx.emit("command_end", {
                        "command": "[background command]",
                        "job_id": job_id,
                        "tool_call_id": original_call_id,
                        "exit_code": result.get("exit_code"),
                        "success": command_success,
                        "stdout": result.get("stdout", ""),
                        "stderr": result.get("stderr", ""),
                        "timed_out": False,
                        "duration_sec": None,
                    })
            return result
        result = original(**kwargs)
        if tool == "stop_command" and isinstance(result, Mapping) and result.get("finalization_error"):
            result = dict(result)
            result["success"] = False
            result["error_code"] = "command_finalization_failed"
            result["error"] = "Command finalization failed"
        if tool == "read_command_output" and isinstance(result, Mapping):
            result = self._normalize_background_read_result(result)
        if tool == "stop_command":
            self.ctx.background_jobs.discard(job_id)
        if tool == "stop_command" or (
            tool == "read_command_output" and isinstance(result, Mapping) and result.get("status") == "exited"
        ):
            original_call_id = self._background_call_ids.pop(job_id, None)
            if original_call_id:
                command_success = (
                    result.get("exit_code") == 0
                    and not result.get("finalization_error")
                )
                if not command_success:
                    self.ctx.counters["commands_failed"] += 1
                self.ctx.emit("command_end", {
                    "command": "[background command]",
                    "job_id": job_id,
                    "tool_call_id": original_call_id,
                    "exit_code": result.get("exit_code"),
                    "success": command_success,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "timed_out": False,
                    "duration_sec": None,
                })
        return result

    @staticmethod
    def _normalize_background_read_result(result: Mapping[str, Any]) -> dict[str, Any]:
        """Expose the owned process outcome instead of retrieval success."""

        normalized = dict(result)
        status = str(normalized.get("status") or "").strip().lower()
        exit_code = normalized.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            normalized.setdefault("return_code", exit_code)
        if normalized.get("finalization_error"):
            normalized["success"] = False
            normalized["error_code"] = "command_finalization_failed"
            normalized["error"] = "Command finalization failed"
        elif status == "exited":
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code == 0:
                normalized["success"] = True
            elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
                normalized["success"] = False
                normalized["error"] = f"Command exited with code {exit_code}"
            else:
                normalized["success"] = False
                normalized["error_code"] = "command_exit_code_missing"
                normalized["error"] = "Command exited without a numeric return code"
        elif status in {"killed", "termination_failed"}:
            normalized["success"] = False
            normalized["error_code"] = "command_terminated"
            normalized["error"] = "Command was stopped before completion"
        else:
            normalized["success"] = True
        return normalized

    def _call_read_command_output(self, original, kwargs):
        return self._owned_job("read_command_output", original, kwargs)

    def _call_write_command_input(self, original, kwargs):
        text = str(kwargs.get("text") or "")
        if _SECRET_PATH_TOKEN_RE.search(text):
            return self._deny("write_command_input", "secret file references are not allowed")
        return self._owned_job("write_command_input", original, kwargs)

    def _call_stop_command(self, original, kwargs):
        return self._owned_job("stop_command", original, kwargs)

    def stop_background_jobs(self) -> None:
        if self.ctx.agent_system_prompt:
            # An accepted job or a running poll is not completion evidence.
            # Terminal observations already remove this call-id obligation.
            self.ctx.unfinished_background_jobs = len(self.ctx.background_jobs & self._background_call_ids.keys())
        if not self.ctx.background_jobs:
            return
        from ..tools.os_operations.background_jobs import get_background_job_registry

        registry = get_background_job_registry()
        for job_id in list(self.ctx.background_jobs):
            with contextlib.suppress(Exception):
                registry.stop(job_id, scope=self.scope if self.scoped_commands else None)
            self.ctx.background_jobs.discard(job_id)
            self._background_call_ids.pop(job_id, None)


def build_task_registry(ctx: TaskRunContext, scope: Any, *, scoped_commands: bool = False):
    """Assemble the task's closed tool set from existing os_operations tools."""

    from ..tools import os_operations as ops
    from ..tools.core import ensure_tool_definitions
    from ..tools.registry import ToolRegistry

    names: list[str] = list(_READ_TOOLS)
    if ctx.spec.mode == "mutate":
        names.extend(_MUTATION_TOOLS)
    if ctx.spec.allow_commands:
        names.extend(_COMMAND_TOOLS)
    guard = ToolGuard(ctx, scope)
    guard.scoped_commands = bool(scoped_commands)
    class TaskToolRegistry(ToolRegistry):
        def execute(self, tool_name: str, **kwargs):
            try:
                return super().execute(tool_name, **kwargs)
            except Exception as exc:
                # Count only a concrete denial from an already-loaded common
                # resource guard. Legacy tools must not import an optional
                # workspace foundation merely to perform this accounting.
                module = sys.modules.get(__package__ + ".tool_mutation_resource_guard")
                denial = getattr(module, "ToolMutationResourceDenied", None)
                if isinstance(denial, type) and isinstance(exc, denial):
                    guard._deny(tool_name, "common mutation resource scope denied")
                raise

    registry = TaskToolRegistry()
    for tool_def in ensure_tool_definitions([getattr(ops, name) for name in names]):
        is_mutation = tool_def.name in _MUTATION_TOOLS
        is_command = tool_def.name == "execute_command"
        registry.register(dataclasses.replace(
            guard.wrap(tool_def),
            owner=(("command_execute" if tool_def.name in _COMMAND_TOOLS else
                "workspace_write" if is_mutation else "workspace_read") if ctx.agent_system_prompt else "local_agent_task"),
            side_effect="mutation" if (is_mutation or is_command) else "none",
            risk="high" if is_command else ("medium" if is_mutation else "low"),
            requires_approval=is_mutation or is_command,
            supports_parallel=False,
        ))
    for tool_def in ctx.extra_tools:
        # The scoped MCP owner supplies closed, authority-bound definitions.
        # Never let an external name replace an existing guarded local tool.
        if tool_def.name in {item.name for item in registry.get_all()}:
            raise LocalAgentTaskError("tool_name_conflict", "duplicate task tool name", 409)
        def wrap_extra(definition):
            original = definition.function
            name = definition.name

            @functools.wraps(original)
            def guarded_extra(**kwargs):
                ctx.check_running()
                ctx.counters["tool_calls"] += 1
                repeated = guard._repeat_check(name, kwargs)
                if repeated:
                    return guard._deny(name, repeated)
                return guard._record(name, original(**kwargs))

            return dataclasses.replace(definition, function=guarded_extra, supports_parallel=False)

        registry.register(wrap_extra(tool_def))
    return registry, guard


# Request text given to AoiTalk's tool policy.  The caller's explicit ``mode``
# is the authority for mutation (tool set, run scope, command guard, post-run
# Git comparison).  Passing goal/constraint prose would let the policy's
# natural-language heuristics ("変更しない", "do not change", "read only")
# block even inspection commands and verified in-workspace edits.
POLICY_REQUEST_TEXT = "Delegated local agent task; the execution mode is enforced by the task runtime."

REPORT_SCHEMA_HINT = (
    '{"status": "completed|failed|blocked", "summary": "<=3 sentences", '
    '"checks": {"<check name e.g. build/tests/adb_install/device_verification>": "pass|fail|skipped|not_run"}, '
    '"files_changed": ["relative/path"], "notes": "optional"}'
)


def build_system_prompt(spec: LocalAgentTaskSpec) -> str:
    if spec.mode == "read_only":
        workflow = (
            "Inspect only what is needed for the requested goal with the allowed tools, "
            "then report the observed facts and stop. Do not create or edit files, "
            "and do not invent additional deliverables or completion criteria."
        )
    elif spec.allow_commands:
        workflow = (
            "Work step by step with the tools: inspect, act, then verify results with a "
            "command or by reading files."
        )
    else:
        # CDS and other file-only callers inherit the Local Agent Task round budget
        # (default 40). Open-ended re-reads burn that budget before the required
        # final JSON report is emitted; independent install/build/test runs outside
        # this loop when commands are disabled.
        workflow = (
            "Work step by step with the file tools: inspect only what you need, make the "
            "required edits, then stop. Do not re-read the same path repeatedly, and do "
            "not keep editing after completion criteria are already met. Independent "
            "install/build/test verification runs outside this task."
        )
    rules = [
        "You are AoiTalk's local coding/operations agent running unattended on the user's PC.",
        f"Workspace (all paths are relative to it): {spec.workspace}",
        f"Mode: {spec.mode}." + (" You may read files and run inspection commands only; do not modify files."
                                 if spec.mode == "read_only" else
                                 " You may edit files inside the workspace with the file tools."),
        workflow,
        "Never discard or overwrite existing uncommitted changes. Never switch branches, stash, reset, clean, "
        "rebase, push or rewrite Git history. Never read .env files, keys or credentials, and never print secrets.",
        "Do not touch files unrelated to the goal. Prefer small, targeted edits (edit_file) over rewriting files.",
        "Use run_in_background=true only for servers/watchers; ordinary builds/tests run in the foreground.",
        "When a tool is blocked by policy, do not retry the same call; choose a permitted approach or report blocked.",
        "As soon as the goal and completion criteria are satisfied, stop calling tools and finish with a short "
        "plain-text summary followed by exactly one JSON object on the last lines: "
        + REPORT_SCHEMA_HINT,
    ]
    if spec.allow_commands:
        rules.append(
            "For execute_command, omit working_directory or use '.' for the workspace root. "
            "For a subdirectory, use an existing workspace-relative directory; do not repeat the workspace basename."
        )
    if not spec.allow_commands:
        rules.append(
            "Command execution is disabled for this task; use only the file tools. "
            "Do not spend the tool-round budget re-checking files that already satisfy the criteria."
        )
        if spec.mode == "read_only":
            rules.append(
                "Final JSON status must be completed once the requested read-only goal "
                "and explicit completion criteria are satisfied. Do not add unrequested "
                "artifact requirements. Mark checks that do not apply as not_run or skipped."
            )
        else:
            rules.append(
                "Final JSON status must be \"completed\" once the goal and completion criteria "
                "(deliverable files, README, report) are satisfied. Independent install/build/test "
                "and browser checks run outside this task — never set status to blocked or failed "
                "only because you could not run commands or a browser; mark those checks as "
                "not_run or skipped instead."
            )
    if spec.allow_adb:
        rules.append("adb is available for install/logcat/inspection; destructive adb operations are blocked.")
    if spec.allow_git_commit:
        rules.append("You may `git add <explicit paths>` and `git commit -m` only the files you changed.")
    if spec.allow_git_push:
        rules.append("You may run an ordinary `git push` to a configured remote with an explicit branch refspec; force, delete, mirror, tags, and config changes remain blocked.")
    if spec.provider == "codex-cli" and spec.allow_commands:
        rules.append(
            "This scoped native Codex task exposes execute_command through the declared task tool schema. "
            "The worker shell is bash/POSIX; use shell=\"bash\" for POSIX syntax (for example, node, npm, "
            "pytest, and &&). Do not send PowerShell or cmd.exe syntax to this scoped worker. "
            "A command's non-zero return_code is a failed command result; inspect its bounded stdout/stderr "
            "and continue only when the task contract permits it."
        )
    return "\n".join(rules)


def build_user_prompt(spec: LocalAgentTaskSpec, baseline: Mapping[str, Any]) -> str:
    parts = [f"Goal:\n{spec.goal}"]
    if spec.constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {item}" for item in spec.constraints))
    if spec.completion_criteria:
        parts.append("Completion criteria:\n" + "\n".join(f"- {item}" for item in spec.completion_criteria))
    if baseline.get("is_git"):
        dirty = list((baseline.get("entries") or {}).keys())
        parts.append(
            "Git baseline (captured by the runtime): branch="
            f"{baseline.get('branch')} head={str(baseline.get('head') or '')[:12]} "
            f"pre-existing uncommitted paths={len(dirty)}"
            + (" (" + ", ".join(dirty[:20]) + (" ..." if len(dirty) > 20 else "") + ")" if dirty else "")
            + ". Leave pre-existing changes intact."
        )
    parts.append(f"Budget: at most {spec.max_tool_rounds} tool rounds; command timeout {spec.command_timeout_sec}s.")
    return "\n\n".join(parts)


def _timeout_detail(ctx: "TaskRunContext") -> str:
    """Explain a wall-clock timeout, separating the first (model-load) response."""

    limit = ctx.spec.timeout_sec
    first = ctx.first_llm_response_seconds
    if first is None:
        return (f"wall-clock timeout ({limit}s) reached before the first model response; "
                "a cold local model load is counted in this wait")
    later = max(0, int(ctx.counters.get("llm_calls", 0)) - 1)
    return (f"wall-clock timeout ({limit}s) reached; the first model response took {first}s "
            f"(a cold local model load is included) and {later} later model calls used the rest")


def parse_report(text: str) -> Optional[dict[str, Any]]:
    """Extract the last JSON report object from the final model message."""

    if not text:
        return None
    decoder = json.JSONDecoder()
    found = None
    for index in [m.start() for m in re.finditer(r"\{", text)]:
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("status") in REPORT_STATUSES:
            found = value
    if found is None:
        return None
    checks = {}
    raw_checks = found.get("checks")
    if isinstance(raw_checks, Mapping):
        for key, value in list(raw_checks.items())[:20]:
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:60]
            checks[name] = value if value in CHECK_VALUES else "unknown"
    files = [str(item)[:300] for item in (found.get("files_changed") or []) if isinstance(item, str)][:100]
    return {
        "status": found["status"],
        "summary": str(found.get("summary") or "")[:2000],
        "checks": checks,
        "files_changed": files,
        "notes": str(found.get("notes") or "")[:2000],
    }


class LocalAgentTaskRunner:
    """Run one task to completion inside the calling (worker) thread."""

    def __init__(
        self, config: Any, *, backend_factory: Callable[[], Any] | None = None,
        collect_git_metadata: bool = True,
        harness_execution_scope: Any | None = None,
        codex_preflight: Callable[[TaskRunContext], None] | None = None,
        codex_profiles: Any | None = None,
    ):
        self.config = config
        self.backend_factory = backend_factory
        self.codex_preflight = codex_preflight
        from .codex_app_profiles import CodexAppProfiles
        self.codex_profiles = codex_profiles if codex_profiles is not None else CodexAppProfiles()
        # Trusted caller opt-out for untrusted customer repositories.  Even
        # git status can execute repository-configured fsmonitor/filter code.
        # Ordinary local developer tasks retain their existing Git checks.
        self.collect_git_metadata = collect_git_metadata
        # Only a server-issued HarnessExecutionScope may opt this runner into
        # scoped shell execution.  The normal local task path remains
        # host-command compatible for existing operator workflows.
        self.harness_execution_scope = harness_execution_scope

    def resolve_route(self, spec: LocalAgentTaskSpec) -> dict[str, str]:
        route = resolve_task_route(self.config, spec)
        if route["provider"] == "codex-cli" and spec.allow_commands and self.harness_execution_scope is None:
            raise LocalAgentTaskError("codex_scope_required", "Codex command tasks require a server-issued execution scope", 409)
        if route["provider"] == "codex-cli" and self.codex_preflight is None:
            raise LocalAgentTaskError("codex_preflight_required", "Codex task route has not passed host preflight", 409)
        return route

    def _create_backend(self, ctx: TaskRunContext):
        ctx.check_running()
        if ctx.spec.codex_connection == "app_profile" and ctx.codex_profile is None:
            from .codex_app_profiles import CodexProfileError
            raise CodexProfileError("codex_profile_server_binding_required")
        if self.backend_factory is not None:
            return self.backend_factory()
        route = self.resolve_route(ctx.spec)
        if route["provider"] == "codex-cli":
            # Trusted server hook must be bounded and cancellation-aware via ctx.
            self.codex_preflight(ctx)
            ctx.check_running()
            from ..llm.codex_task_completion import CodexTaskCompletionClient

            client_holder: dict[str, Any] = {}

            def record_codex_usage(payload: Mapping[str, Any]) -> None:
                """Bridge trusted app-server usage to both ledgers."""

                if ctx.provider_usage_recorder is not None:
                    try:
                        ctx.provider_usage_recorder(payload)
                    except Exception:
                        logger.debug("native provider usage telemetry callback failed", exc_info=True)
                client = client_holder.get("client")
                if client is None:
                    return
                try:
                    from ..llm.conversation_context import persist_usage_sync

                    persist_usage_sync(
                        client,
                        provider="codex-cli",
                        model=route["model"],
                        usage=payload,
                        requested_model=route["model"],
                        resolved_model=route["model"],
                        request_type="codex_task",
                    )
                except Exception:
                    logger.debug("native provider usage persistence failed", exc_info=True)

            client_kwargs = {
                "model": route["model"], "effort": route["effort"], "workspace": ctx.spec.workspace,
                "timeout_seconds": ctx.remaining(), "cancelled": ctx.cancel_event.is_set,
                "task_mode": ctx.spec.mode,
                "checkpoint": ctx.check_running,
                "user_id": ctx.user_id,
                "project_id": ctx.project_id,
                "instruction_review": ctx.instruction_review,
                "profile": ctx.codex_profile,
            }
            if ctx.provider_usage_recorder is not None:
                client_kwargs["usage_recorder"] = record_codex_usage
            client = CodexTaskCompletionClient(**client_kwargs)
            client_holder["client"] = client
            return client
        return AoiTalkLocalLLMBackend(
            self.config, **route, strict_route=ctx.spec.provider is not None,
            user_id=ctx.user_id, project_id=ctx.project_id,
        )

    def _snapshot(self, workspace: str) -> dict[str, Any]:
        if not self.collect_git_metadata:
            return {"is_git": False, "git_metadata_skipped": "untrusted_workspace"}
        return git_snapshot(workspace)

    def run_inline(
        self,
        spec: LocalAgentTaskSpec,
        *,
        task_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        cancel_event: threading.Event | None = None,
        limits: LocalAgentTaskLimits | None = None,
        emit: Callable[[str, Mapping[str, Any]], None] | None = None,
        update: Callable[[Mapping[str, Any]], None] | None = None,
        provider_usage_recorder: Callable[[Mapping[str, Any]], None] | None = None,
        _instruction_review: ReviewedHostInstructions | None = None,
        _instruction_review_boot_id: str | None = None,
        extra_tools: Sequence[Any] = (),
        agent_system_prompt: str = "",
        execution_checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Run one task synchronously with the same durable-task runtime.

        AgentWork already owns the durable claim/lease.  This entry point is
        intentionally an inline adapter for that coordinator: it does not
        create a second queue or persistence record, but it still uses the
        exact ``run()`` path, including scope, registry, guard and final
        Git comparison.
        """

        if getattr(spec, "mode", None) not in MODES:
            return {
                "status": "failed",
                "failure_reason": "invalid_task_mode",
                "changed_files": [],
            }

        if not local_agent_tasks_enabled(self.config):
            return {
                "status": "failed",
                "failure_reason": "local_worker_unavailable",
                "failure_detail": "local agent tasks are disabled for this runtime",
                "changed_files": [],
            }

        resolved_task_id = str(task_id or uuid.uuid4().hex)
        resolved_user_id = str(user_id) if user_id is not None else None
        if _instruction_review is not None:
            if not isinstance(_instruction_review, ReviewedHostInstructions):
                return {"status": "failed", "failure_reason": "instruction_review_invalid", "changed_files": []}
            binding = _instruction_review.binding
            expected_fingerprint = spec.fingerprint()
            if (
                binding.get("task_id") != resolved_task_id
                or binding.get("owner") != resolved_user_id
                or binding.get("task_fingerprint") != expected_fingerprint
                or not str(binding.get("boot_id") or "").strip()
                or (_instruction_review_boot_id is not None and binding.get("boot_id") != _instruction_review_boot_id)
            ):
                return {"status": "failed", "failure_reason": "instruction_review_binding_invalid", "changed_files": []}
            try:
                _instruction_review.check()
            except CodexTaskProtocolError:
                return {"status": "failed", "failure_reason": "instruction_review_invalidated", "changed_files": []}
        ctx = TaskRunContext(
            task_id=resolved_task_id,
            spec=spec,
            limits=limits or LocalAgentTaskLimits.from_config(self.config),
            cancel_event=cancel_event or threading.Event(),
            emit=emit or (lambda _kind, _payload: None),
            update=update or (lambda _fields: None),
            redactor=SecretRedactor(),
            user_id=resolved_user_id,
            project_id=str(project_id) if project_id is not None else None,
            provider_usage_recorder=provider_usage_recorder,
            extra_tools=tuple(extra_tools),
            agent_system_prompt=str(agent_system_prompt),
            execution_checkpoint=execution_checkpoint,
            # Trusted internal callers may bind a deployment-issued host
            # instruction receipt to this inline run.  Public task requests
            # still have no field that can manufacture or adopt one.
            instruction_review=_instruction_review,
        )
        original_update = ctx.update
        ctx.update = lambda fields: original_update({**dict(fields), "counters": dict(ctx.counters)})
        result = self.run(ctx)
        ctx.update({"counters": dict(ctx.counters)})
        return result

    def run(self, ctx: TaskRunContext) -> dict[str, Any]:
        if getattr(ctx.spec, "mode", None) not in MODES:
            return {
                "status": "failed",
                "failure_reason": "invalid_task_mode",
                "changed_files": [],
            }
        from .codex_app_profiles import CodexProfileError
        from ..llm.generation_budget import GenerationBudget, current_generation_budget
        release = lambda: None
        ctx.started_monotonic = time.monotonic()
        ctx.deadline = ctx.started_monotonic + ctx.spec.timeout_sec
        budget_token = current_generation_budget.set(GenerationBudget(
            ctx.spec.timeout_sec,
            started_at=ctx.deadline - ctx.spec.timeout_sec,
            cancelled=ctx.cancel_event,
            interruptible_transport=True,
            parent=current_generation_budget.get(),
        ))
        try:
            self.codex_profiles.assert_no_secret_scope_overlap([Path(ctx.spec.workspace)])
            if ctx.spec.codex_connection == "app_profile":
                if ctx.codex_profile_receipt is None or ctx.instruction_review is not None:
                    raise CodexProfileError("codex_profile_server_binding_required")
                ctx.codex_profile = self.codex_profiles.lease(ctx.user_id, receipt=ctx.codex_profile_receipt)
                ctx.codex_profile.check(owner=ctx.user_id, read_roots=[Path(ctx.spec.workspace)])
                release = ctx.codex_profile.own_cleanup(ctx.cancel_event.set)
            return self._run_bound(ctx)
        except CodexProfileError as exc:
            return {"status": "failed", "failure_reason": exc.code}
        finally:
            try:
                release()
            finally:
                current_generation_budget.reset(budget_token)

    def _run_bound(self, ctx: TaskRunContext) -> dict[str, Any]:
        from ..llm.generation_policy import (
            GenerationProfile,
            generation_policy_for_profile,
            reset_current_generation_policy,
            set_current_generation_policy,
        )
        from ..security.agent_run_scope import AgentRunScope, run_scope_context
        from ..security.harness_execution_scope import (
            HarnessExecutionScope,
            get_current_harness_execution_scope,
            harness_execution_scope_context,
        )

        spec = ctx.spec
        if spec.mode not in MODES:
            return {
                "status": "failed",
                "failure_reason": "invalid_task_mode",
                "changed_files": [],
            }
        baseline = self._snapshot(spec.workspace)
        ctx.update({"baseline": _public_snapshot(baseline), "latest_activity": "baseline captured"})
        if spec.expected_branch and baseline.get("branch") != spec.expected_branch:
            return {"status": "failed", "failure_reason": "branch_mismatch",
                    "failure_detail": f"expected {spec.expected_branch}, found {baseline.get('branch')}"}

        trusted_scope = self.harness_execution_scope or get_current_harness_execution_scope()
        if trusted_scope is not None:
            if not isinstance(trusted_scope, HarnessExecutionScope):
                raise TypeError("harness_execution_scope must be server-issued")
            expected_root = Path(trusted_scope.canonical_root).resolve()
            requested_root = Path(spec.workspace).resolve()
            if expected_root != requested_root:
                raise RuntimeError("harness execution scope does not match the task workspace")
            scope = trusted_scope.to_agent_run_scope()
            expected_level = "write" if spec.mode == "mutate" else "read"
            if scope.workspace_access_level != expected_level:
                raise RuntimeError("harness execution scope access level does not match the task mode")
        else:
            scope = AgentRunScope.for_repository(
                spec.workspace,
                run_id=ctx.task_id,
                workspace_access_level="write" if spec.mode == "mutate" else "read",
            )
        self.codex_profiles.assert_no_secret_scope_overlap([*scope.read_roots, *scope.write_roots])
        if ctx.codex_profile is not None:
            ctx.codex_profile.check(owner=ctx.user_id, read_roots=[*scope.read_roots, *scope.write_roots])
        registry, guard = build_task_registry(
            ctx,
            scope,
            scoped_commands=trusted_scope is not None,
        )
        backend = None
        outcome: dict[str, Any] = {}
        policy_token = set_current_generation_policy(
            generation_policy_for_profile(GenerationProfile.AUTONOMOUS_WORK)
        )
        try:
            backend = self._create_backend(ctx)
            with contextlib.ExitStack() as stack:
                if trusted_scope is not None:
                    stack.enter_context(harness_execution_scope_context(trusted_scope))
                stack.enter_context(run_scope_context(scope))
                outcome = self._loop(ctx, backend, registry, baseline)
        except _TaskStop as stop:
            outcome = {"stop": stop.reason}
        except Exception as exc:  # provider / runtime failure
            from ..llm.generation_budget import current_generation_budget
            from ..llm.generation_cancellation import GenerationCancelled

            try:
                ctx.check_running()
                budget = current_generation_budget.get()
                if budget is not None:
                    budget.checkpoint()
            except _TaskStop as stop:
                outcome = {"stop": stop.reason}
            except GenerationCancelled:
                outcome = {"stop": "cancelled"}
            except TimeoutError:
                outcome = {"stop": "timeout"}
            else:
                logger.warning("local agent task %s failed: %s", ctx.task_id, type(exc).__name__)
                outcome = {"error": exc}
        finally:
            reset_current_generation_policy(policy_token)
            # The task's background registry is scope-owned.  Rebind both
            # layers during cleanup so cancellation cannot leave a scoped WSL
            # process alive merely because the main loop already exited its
            # ContextVar contexts.
            if trusted_scope is not None:
                with contextlib.ExitStack() as cleanup_stack:
                    cleanup_stack.enter_context(harness_execution_scope_context(trusted_scope))
                    cleanup_stack.enter_context(run_scope_context(scope))
                    guard.stop_background_jobs()
            else:
                # Legacy/manual host jobs are intentionally ownerless and
                # must be cleaned up outside a synthetic AgentRunScope.
                guard.stop_background_jobs()
            with contextlib.suppress(Exception):
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        return self._finalize(ctx, baseline, outcome)

    def _loop(self, ctx: TaskRunContext, backend: Any, registry: Any, baseline: Mapping[str, Any]) -> dict[str, Any]:
        from ..llm.harness_instructions import with_common_harness_instructions
        from ..llm.unified_turn_runtime import run_openai_compatible_turn_loop
        from ..tools.adapters import OpenAIAPIAdapter

        spec = ctx.spec
        messages = [
            {"role": "system", "content": with_common_harness_instructions(
                "\n\n".join(part for part in (ctx.agent_system_prompt, build_system_prompt(spec)) if part))},
            {"role": "user", "content": build_user_prompt(spec, baseline)},
        ]
        tools = OpenAIAPIAdapter.convert_all(registry.get_all())
        if ctx.agent_system_prompt:
            ctx.update({"execution_state": "loading_model", "current_step": "loading_model"})
        request = backend.build_request(messages, tools)
        describe = getattr(backend, "describe", None)
        if callable(describe):
            with contextlib.suppress(Exception):
                ctx.update({"llm": describe()})

        def create_completion(kwargs: dict[str, Any]) -> Any:
            ctx.check_running()
            ctx.counters["llm_calls"] += 1
            ctx.update({"current_step": f"llm call {ctx.counters['llm_calls']}",
                        "latest_activity": "waiting for local LLM"})
            response = backend.complete(kwargs)
            if ctx.first_llm_response_seconds is None and ctx.started_monotonic:
                ctx.first_llm_response_seconds = round(time.monotonic() - ctx.started_monotonic, 1)
            if ctx.agent_system_prompt:
                ctx.update({"execution_state": "running"})
            ctx.check_running()
            ctx.emit("llm_response", {"call": ctx.counters["llm_calls"],
                                      "tool_calls": len(getattr(response.choices[0].message, "tool_calls", None) or [])})
            return response

        def on_event(kind: str, payload: Mapping[str, Any]) -> None:
            if kind == "assistant_text":
                text = str(payload.get("text") or payload.get("content") or "")
                if text.strip():
                    ctx.emit("assistant_note", {"text": text})
            elif kind == "tool_start":
                # Counts every model tool request, including ones AoiTalk's
                # tool policy blocks before they reach the ToolGuard.
                ctx.counters["tool_requests"] = ctx.counters.get("tool_requests", 0) + 1
                ctx.emit("tool_start", payload)
                ctx.update({"current_step": f"tool {payload.get('tool')}",
                            "latest_activity": f"running {payload.get('tool')}"})
            elif kind == "tool_end":
                ctx.emit("tool_end", payload)

        first = create_completion(request)
        result = run_openai_compatible_turn_loop(
            initial_messages=messages,
            assistant_message=first.choices[0].message,
            api_kwargs=request,
            registry=registry,
            create_completion=create_completion,
            log_prefix="LocalAgentTask",
            max_rounds=spec.max_tool_rounds,
            max_tool_result_chars=ctx.limits.tool_result_chars,
            message_content=getattr(backend, "message_content", None),
            config=self.config,
            user_input=POLICY_REQUEST_TEXT,
            enforce_tool_policy=True,
            event_callback=on_event,
            restore_tool_arguments=getattr(backend, "restore_tool_arguments", None),
        )
        return {"final_output": result.final_output or "", "stopped_reason": result.stopped_reason,
                "rounds": result.rounds}

    def _finalize(self, ctx: TaskRunContext, baseline: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
        spec = ctx.spec
        after = self._snapshot(spec.workspace)
        git = compare_git_snapshots(baseline, after)
        if git.get("is_git"):
            changed = git["task_changed_files"]
        else:
            changed = sorted({str(Path(p).relative_to(spec.workspace)) if _path_within(Path(p), Path(spec.workspace)) else p
                              for p in ctx.touched_files})
        final_text = ctx.redactor.text(outcome.get("final_output") or "", 6000)
        report = parse_report(final_text)
        status, reason, detail = "completed", None, None
        if "stop" in outcome:
            reason = outcome["stop"]
            status = "cancelled" if reason == "cancelled" else "failed"
            detail = {
                "timeout": _timeout_detail(ctx),
                "repeated_action": "the model repeated identical actions without progress",
                "no_progress": f"{ctx.limits.failure_streak_limit} consecutive tool failures",
                "cancelled": "cancel requested",
            }.get(reason, reason)
        elif "error" in outcome:
            status, reason = "failed", "llm_or_runtime_error"
            detail = ctx.redactor.text(f"{type(outcome['error']).__name__}: {outcome['error']}", 500)
        elif ctx.agent_system_prompt and outcome.get("stopped_reason") in {"approval_required", "awaiting_approval"}:
            status, reason, detail = "waiting_approval", "approval_required", "Waiting for canonical tool approval"
        elif outcome.get("stopped_reason") == "max_rounds":
            status, reason, detail = "failed", "max_tool_rounds", f"stopped after {spec.max_tool_rounds} tool rounds"
        elif outcome.get("stopped_reason") == "redundant_tool_call_repeated":
            # AoiTalk's turn loop suppressed a duplicate successful call and the
            # model still asked for it again: the same runaway signal.
            status, reason, detail = "failed", "repeated_action", "the model kept repeating an already completed call"
        elif report is None:
            if ctx.agent_system_prompt:
                status, reason, detail = "failed", "completion_unverified", "No verifiable task completion report was produced"
            else:
                reason = None
        elif report["status"] == "failed":
            status, reason, detail = "failed", "agent_reported_failure", report["summary"]
        elif report["status"] == "blocked":
            status, reason, detail = "failed", "agent_blocked", report["summary"]
        if git.get("is_git") and status != "cancelled":
            if git.get("branch_changed") or (git.get("head_changed") and not spec.allow_git_commit):
                status, reason, detail = "failed", "git_state_changed", "branch or HEAD changed during the task"
            elif git.get("preexisting_changes_reverted"):
                status, reason, detail = "failed", "preexisting_changes_reverted", ", ".join(git["preexisting_changes_reverted"][:20])
        if spec.mode == "read_only" and changed and status != "cancelled":
            status, reason, detail = "failed", "read_only_violation", ", ".join(changed[:20])
        checks = dict(report["checks"]) if report else {}
        if ctx.agent_system_prompt and status == "completed":
            if ctx.unfinished_background_jobs:
                status, reason, detail = "failed", "background_completion_unverified", "A background command had no observed terminal result before task cleanup"
            elif ctx.verified_tool_results < 1:
                status, reason, detail = "failed", "completion_unverified", "No successful guarded tool result supports this workspace task"
            elif "fail" in checks.values():
                status, reason, detail = "failed", "agent_reported_check_failure", "The completion report includes a failed check"
        summary_lines = [status]
        summary_lines.append(f"tool_calls: {ctx.counters['tool_calls']} llm_calls: {ctx.counters['llm_calls']}")
        summary_lines.append(f"commands: {ctx.counters['commands']} (failed {ctx.counters['commands_failed']})")
        changed_label = "structured_files_changed" if ctx.agent_system_prompt and spec.allow_commands else "files_changed"
        summary_lines.append(f"{changed_label}: {len(changed)}")
        for key, value in checks.items():
            summary_lines.append(f"{key}: {value}")
        if git.get("is_git"):
            pre = len(git.get("preexisting_dirty") or [])
            state = "clean" if not git.get("dirty_after") else f"dirty ({len(changed)} task files, {pre} pre-existing)"
            summary_lines.append(f"git_status: {state}")
        if reason:
            summary_lines.append(f"failure_reason: {reason}")
        return {
            "status": status,
            "failure_reason": reason,
            "failure_detail": ctx.redactor.text(detail, 1000) if detail else None,
            "completion_summary": (report or {}).get("summary") or final_text[-1500:],
            "summary_text": "\n".join(summary_lines),
            "result": {
                "stopped_reason": outcome.get("stop") or outcome.get("stopped_reason") or ("error" if "error" in outcome else None),
                "timing": ctx.timing(),
                "rounds": outcome.get("rounds"),
                "report": report,
                "report_parsed": report is not None,
                "checks": checks,
                **({"checks_source": "agent_report", "verified_tool_results": ctx.verified_tool_results,
                    "unfinished_background_jobs": ctx.unfinished_background_jobs,
                    "criteria_verification": "Tool execution is recorded; arbitrary completion criteria are not independently certified"}
                    if ctx.agent_system_prompt else {}),
                "git": {key: value for key, value in git.items() if key != "preexisting_dirty"}
                | {"preexisting_dirty_count": len(git.get("preexisting_dirty") or [])},
                "commands": ctx.command_log[-30:],
                **({"changed_files_complete": False, "changed_files_source": "guarded_structured_file_tools",
                    "command_changes": "See scoped command publication audit; a complete command file-path inventory is not available"}
                    if ctx.agent_system_prompt and spec.allow_commands else {}),
                "final_message": final_text[-4000:],
            },
            "changed_files": changed[:500],
        }


def _public_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not snapshot.get("is_git"):
        return {"is_git": False}
    return {"is_git": True, "branch": snapshot.get("branch"), "head": snapshot.get("head"),
            "dirty_count": len(snapshot.get("entries") or {})}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class LocalAgentTaskManager:
    """Queue + daemon worker threads + persistent records."""

    HEARTBEAT_SEC = 10.0
    CANCEL_JOIN_TIMEOUT_SEC = 10.0

    def __init__(
        self,
        *,
        config: Any,
        store: LocalAgentTaskStore | None = None,
        runner: LocalAgentTaskRunner | None = None,
        limits: LocalAgentTaskLimits | None = None,
        allowed_roots: Sequence[Path] | None = None,
        boot_id: str | None = None,
        host_instruction_owner_id: str | None = None,
    ):
        self.config = config
        self.limits = limits or LocalAgentTaskLimits.from_config(config)
        storage = _cfg(config, "local_agent_tasks.storage_dir", "") or str(_REPO_ROOT / "data" / "local_agent_tasks")
        self.store = store or LocalAgentTaskStore(Path(storage))
        self.runner = runner or LocalAgentTaskRunner(config)
        self.allowed_roots = list(allowed_roots) if allowed_roots is not None else default_allowed_roots(config)
        self.boot_id = boot_id or uuid.uuid4().hex
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._task_threads: dict[str, threading.Thread] = {}
        self._task_done_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._started = False
        self._submit_lock = threading.Lock()
        self._instruction_reviews: dict[str, dict[str, Any]] = {}
        try:
            self._host_instruction_owner_id = str(uuid.UUID(str(host_instruction_owner_id)))
        except (ValueError, TypeError, AttributeError):
            self._host_instruction_owner_id = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._recover()
            for index in range(max(1, self.limits.max_concurrency)):
                thread = threading.Thread(target=self._worker, name=f"local-agent-task-{index}", daemon=True)
                thread.start()
                self._threads.append(thread)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            threads = list(self._threads)
            for task_id, event in list(self._cancel_events.items()):
                with contextlib.suppress(LocalAgentTaskError):
                    self._mutate(task_id, {"cancel_reason": "aoitalk_shutdown"})
                event.set()
            for _ in threads:
                self._queue.put(None)
        deadline = time.monotonic() + self.CANCEL_JOIN_TIMEOUT_SEC
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _recover(self) -> None:
        for record in self.store.list():
            if record.get("executor") == "aoitalk_agent":
                continue
            status = record.get("status")
            if status == "running" or (status == "queued" and record.get("cancel_requested")):
                record.update(status="failed" if status == "running" else "cancelled",
                              failure_reason="interrupted_by_restart" if status == "running" else "cancelled",
                              failure_detail="AoiTalk restarted while the task was running" if status == "running" else None,
                              finished_at=_now(), updated_at=_now())
                self.store.save(record)
                self.store.append_event(record["task_id"], {"ts": _now(), "type": "status", "status": record["status"],
                                                            "reason": record["failure_reason"]})
            elif status == "queued":
                self._queue.put(record["task_id"])

    # -- public API ------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": self._started,
            "boot_id": self.boot_id,
            "model": AoiTalkLocalLLMBackend.resolve_model(self.config),
            "supports_task_routes": True,
            "canonical_agent_required": True,
            "default_agent_id": _cfg(self.config, "local_agent_tasks.default_agent_id", None),
            "common_runtime_connected": getattr(self, "canonical", None) is not None,
            "host_instruction_review_owner_bound": self._host_instruction_owner_id is not None,
            "codex_app_profile_connection_enabled": getattr(getattr(self.runner, "codex_profiles", None), "owner_id", None) is not None,
            "codex_app_profile_connector_available": getattr(getattr(self.runner, "codex_profiles", None), "_connector", None) is not None,
            "supported_providers": ["openai_compatible_local", "ollama"] + (
                ["codex-cli"] if self.runner.codex_preflight is not None else []),
            "default_route": {"provider": "openai_compatible_local",
                              "model": AoiTalkLocalLLMBackend.resolve_model(self.config), "effort": ""},
            "route_blockers": {} if self.runner.codex_preflight is not None else {
                "codex-cli": "codex_preflight_required"},
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "max_concurrency": self.limits.max_concurrency,
            "limits": dataclasses.asdict(self.limits),
            "queued": self._queue.qsize(),
        }

    def submit(self, payload: Mapping[str, Any], *, owner: str, idempotency_key: str | None = None
               ) -> tuple[dict[str, Any], bool]:
        spec = validate_task_request(payload, allowed_roots=self.allowed_roots, limits=self.limits)
        if spec.agent_id is not None:
            raise LocalAgentTaskError("canonical_runtime_unavailable", "Agent requests require common AgentWork admission", 409)
        from .codex_app_profiles import CodexProfileError
        try:
            profiles = getattr(self.runner, "codex_profiles", None)
            if profiles is not None:
                profiles.assert_no_secret_scope_overlap(self.allowed_roots)
        except CodexProfileError as exc:
            raise LocalAgentTaskError(exc.code, "タスクの範囲に専用Codex秘密保存領域が含まれています", exc.http_status) from None
        route = self.runner.resolve_route(spec)
        key = idempotency_key or payload.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key)):
            raise LocalAgentTaskError("invalid_input", "idempotency_key is invalid")
        fingerprint = spec.fingerprint()
        with self._submit_lock:
            if key:
                existing = self.store.find_key(owner, key)
                if existing:
                    if existing.get("fingerprint") != fingerprint:
                        raise LocalAgentTaskError("idempotency_conflict",
                                                  "idempotency_key was already used for a different request",
                                                  http_status=409)
                    return self.view(self.store.load(existing["task_id"])), False
            profile_receipt = None
            if spec.codex_connection == "app_profile":
                try:
                    lease = self.runner.codex_profiles.lease(owner)
                    lease.check(owner=owner, read_roots=self.allowed_roots)
                    profile_receipt = lease.receipt()
                except CodexProfileError as exc:
                    raise LocalAgentTaskError(exc.code, "専用Codex接続が未許可・未接続または失効しています", exc.http_status) from None
            task_id = uuid.uuid4().hex
            now = _now()
            record = {
                "task_id": task_id,
                "status": "queued",
                "executor": "aoitalk_local",
                "owner": owner,
                "request": spec.to_dict(),
                "llm": route,
                "idempotency_key": key,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "last_progress_at": now,
                "heartbeat_at": None,
                "boot_id": self.boot_id,
                "counters": {},
                "current_step": "queued",
                "latest_activity": "queued",
                "changed_files": [],
                "cancel_requested": False,
                "failure_reason": None,
                "failure_detail": None,
                "completion_summary": None,
                "summary_text": None,
                "result": None,
                "event_count": 0,
            }
            if profile_receipt is not None:
                record["codex_profile_binding"] = profile_receipt
            self.store.create(record)
            if key:
                self.store.put_key(owner, key, task_id, fingerprint)
        self.store.append_event(task_id, {"ts": now, "type": "status", "status": "queued"})
        self._queue.put(task_id)
        return self.view(record), True

    def _assert_instruction_review_owner(self, owner: str) -> None:
        # Admin host-command access is not permission to disclose the host's
        # private global instructions. The deployment operator binds one owner.
        if self._host_instruction_owner_id is None or owner != self._host_instruction_owner_id:
            raise LocalAgentTaskError("instruction_review_owner_required", "このホストの指示閲覧が許可された本人IDが必要です", 403)

    def preview_instruction_review(self, payload: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
        self._assert_instruction_review_owner(owner)
        from ..llm.codex_instruction_review import (
            REVIEW_TTL_SEC, cli_snapshot, host_snapshot, review_digest, snapshot_metadata,
        )
        spec = validate_task_request(payload, allowed_roots=self.allowed_roots, limits=self.limits)
        if (spec.codex_connection is not None or spec.provider != "codex-cli" or spec.mode != "read_only" or spec.allow_commands
                or spec.allow_adb or spec.allow_git_commit):
            raise LocalAgentTaskError("instruction_review_read_only_required", "指示レビューは明示したCodex読み取り専用タスクのみ利用できます", 409)
        self.runner.resolve_route(spec)
        host, cli = host_snapshot(), cli_snapshot()
        now = _now()
        challenge = {"nonce": uuid.uuid4().hex, "task_id": uuid.uuid4().hex, "owner": owner,
                     "task_fingerprint": spec.fingerprint(), "expires_at": now + REVIEW_TTL_SEC,
                     "execution_expires_at": now + REVIEW_TTL_SEC + spec.timeout_sec,
                     "host": snapshot_metadata(host), "cli": cli, "boot_id": self.boot_id}
        challenge["review_digest"] = review_digest(challenge)
        with self._submit_lock:
            self._instruction_reviews = {k: v for k, v in self._instruction_reviews.items()
                                         if v["expires_at"] > now}
            if len(self._instruction_reviews) >= 100:
                raise LocalAgentTaskError("instruction_review_limit", "指示レビューの受付上限です", 429)
            self._instruction_reviews[challenge["nonce"]] = {**challenge, "spec": spec}
        return {k: v for k, v in {**challenge, "sources": host["sources"], "cli_version": cli["version"]}.items()
                if k not in {"host", "cli", "boot_id"}}

    def instruction_review_status(self, nonce: str, *, owner: str) -> dict[str, Any]:
        self._assert_instruction_review_owner(owner)
        with self._submit_lock:
            challenge = self._instruction_reviews.get(nonce)
            if challenge is None or challenge["owner"] != owner:
                raise LocalAgentTaskError("not_found", "指示レビューが見つかりません", 404)
            task = None
            with contextlib.suppress(LocalAgentTaskError):
                record = self.store.load(challenge["task_id"])
                if record.get("owner") == owner and record.get("instruction_review", {}).get("nonce") == nonce:
                    task = self.view(record)
            return {"task_id": challenge["task_id"], "task": task,
                    "expired": _now() >= challenge["expires_at"], "consumed": task is not None}

    def approve_instruction_review(self, nonce: str, *, owner: str, review_digest: str,
                                   task_fingerprint: str) -> tuple[dict[str, Any], bool]:
        self._assert_instruction_review_owner(owner)
        from ..llm.codex_instruction_review import ReviewedHostInstructions
        with self._submit_lock:
            challenge = self._instruction_reviews.get(nonce)
            if challenge is None or challenge["owner"] != owner:
                raise LocalAgentTaskError("not_found", "指示レビューが見つかりません", 404)
            if (challenge["review_digest"] != review_digest or challenge["task_fingerprint"] != task_fingerprint):
                raise LocalAgentTaskError("instruction_review_conflict", "表示した指示レビューと一致しません", 409)
            # Durable task creation is the consumption record. A lost response
            # can only return this same task; it never grants another turn.
            with contextlib.suppress(LocalAgentTaskError):
                existing = self.store.load(challenge["task_id"])
                if (existing.get("owner") == owner
                        and existing.get("instruction_review", {}).get("nonce") == nonce):
                    return self.view(existing), False
            if _now() >= challenge["expires_at"]:
                raise LocalAgentTaskError("instruction_review_expired", "指示レビューの期限が切れました", 409)
            spec = challenge["spec"]
            route = self.runner.resolve_route(spec)
            receipt = {k: v for k, v in challenge.items() if k != "spec"}
            review = ReviewedHostInstructions(receipt, task_id=challenge["task_id"], owner=owner,
                                               fingerprint=spec.fingerprint(), boot_id=self.boot_id)
            review.check()
            task_id, now = challenge["task_id"], _now()
            record = {"task_id": task_id, "status": "queued", "executor": "aoitalk_local", "owner": owner,
                      "request": spec.to_dict(), "llm": route, "idempotency_key": None,
                      "instruction_review": receipt, "created_at": now, "updated_at": now,
                      "started_at": None, "finished_at": None, "last_progress_at": now, "heartbeat_at": None,
                      "boot_id": self.boot_id, "counters": {}, "current_step": "queued", "latest_activity": "queued",
                      "changed_files": [], "cancel_requested": False, "failure_reason": None,
                      "failure_detail": None, "completion_summary": None, "summary_text": None,
                      "result": None, "event_count": 0}
            # One submit lock covers receipt consumption, durable record and
            # enqueue. Restart recovery rejects the old boot-bound receipt.
            self.store.create(record)
            self.store.append_event(task_id, {"ts": now, "type": "status", "status": "queued"})
            self._queue.put(task_id)
            return self.view(record), True

    def _visible_record(self, task_id: str, owner: str | None, allow_profile: bool = True) -> dict[str, Any]:
        record = self.store.load(task_id)
        if record.get("codex_profile_binding") is not None and not allow_profile:
            raise LocalAgentTaskError("not_found", "task not found", 404)
        if (record.get("executor") == "aoitalk_agent" or record.get("instruction_review") is not None or record.get("codex_profile_binding") is not None) and record.get("owner") != owner:
            raise LocalAgentTaskError("not_found", "task not found", 404)
        return record

    def get(self, task_id: str, *, owner: str | None = None, allow_profile: bool = True) -> dict[str, Any]:
        return self.view(self._visible_record(task_id, owner, allow_profile))

    def list(self, *, limit: int = 20, status: str | None = None, owner: str | None = None, allow_profile: bool = True) -> list[dict[str, Any]]:
        if status is not None and status not in TASK_STATES:
            raise LocalAgentTaskError("invalid_input", "unknown status filter")
        records = [item for item in self.store.list() if (status is None or item.get("status") == status)
                   and ((item.get("executor") != "aoitalk_agent" and item.get("instruction_review") is None and item.get("codex_profile_binding") is None) or item.get("owner") == owner)
                   and (allow_profile or item.get("codex_profile_binding") is None)]
        return [self.view(item, brief=True) for item in records[: max(1, min(int(limit), 100))]]

    def tail(self, task_id: str, *, limit: int = 50, max_bytes: int = 16_000, owner: str | None = None, allow_profile: bool = True) -> dict[str, Any]:
        record = self._visible_record(task_id, owner, allow_profile)
        limit = max(1, min(int(limit), 200))
        max_bytes = max(1024, min(int(max_bytes), 64_000))
        events, truncated = self.store.tail(task_id, limit=limit, max_bytes=max_bytes)
        return {"task_id": task_id, "status": record.get("status"), "events": events,
                "truncated": truncated, "event_count": record.get("event_count", 0)}

    def cancel(self, task_id: str, *, owner: str | None = None, allow_profile: bool = True) -> dict[str, Any]:
        self._visible_record(task_id, owner, allow_profile)
        with self._lock:
            record = self.store.load(task_id)
            if record.get("status") in TERMINAL_STATES:
                return self.view(record)
            record = self._mutate(task_id, {"cancel_requested": True})
            event = self._cancel_events.get(task_id)
            done = self._task_done_events.get(task_id)
            worker = self._task_threads.get(task_id)
            if event is not None:
                event.set()
            elif record.get("status") == "queued":
                record = self._mutate(task_id, {"status": "cancelled", "finished_at": _now(),
                                                "failure_reason": "cancelled", "latest_activity": "cancelled before start"})
                self.store.append_event(task_id, {"ts": _now(), "type": "status", "status": "cancelled"})
        if event is not None and done is not None and worker is not threading.current_thread():
            # The worker is permanent and waits for the next queued job after
            # this task. Wait for this execution's cleanup, never thread exit.
            finished = done.wait(timeout=self.CANCEL_JOIN_TIMEOUT_SEC)
            with self._lock:
                record = self.store.load(task_id)
                if not finished and record.get("status") not in TERMINAL_STATES:
                    record = self._mutate(task_id, {"latest_activity": "cancel requested; worker is still unwinding"})
        return self.view(record)

    # -- views -----------------------------------------------------------
    def view(self, record: Mapping[str, Any], *, brief: bool = False) -> dict[str, Any]:
        now = _now()
        status = record.get("status")
        running = status == "running"
        last_progress = record.get("last_progress_at")
        heartbeat = record.get("heartbeat_at")
        request = record.get("request") or {}
        stall_after = max(300, int(request.get("command_timeout_sec") or 300) + 120)
        seconds_since_progress = round(now - float(last_progress), 1) if last_progress else None
        worker_alive = bool(running and heartbeat and now - float(heartbeat) < self.HEARTBEAT_SEC * 4
                            and record.get("boot_id") == self.boot_id)
        data = {
            "task_id": record.get("task_id"),
            "status": status,
            "executor": record.get("executor", "aoitalk_local"),
            "goal": str(request.get("goal") or "")[:300],
            "workspace": request.get("workspace"),
            "mode": request.get("mode"),
            "created_at": _iso(record.get("created_at")),
            "started_at": _iso(record.get("started_at")),
            "finished_at": _iso(record.get("finished_at")),
            "last_progress_at": _iso(last_progress),
            "seconds_since_progress": seconds_since_progress,
            "current_step": record.get("current_step"),
            "latest_activity": record.get("latest_activity"),
            "counters": record.get("counters") or {},
            "cancel_requested": bool(record.get("cancel_requested")),
            "failure_reason": record.get("failure_reason"),
            "summary_text": record.get("summary_text"),
            "is_terminal": status in TERMINAL_STATES,
            **{key: record.get(key) for key in (
                "agent_id", "agent_revision_id", "agent_run_id", "work_id", "session_id",
                "requested_route", "resolved_route", "execution_state",
            )},
        }
        if running:
            data["worker_alive"] = worker_alive
            data["stalled"] = bool(not worker_alive or (seconds_since_progress or 0) > stall_after)
            data["stall_threshold_sec"] = stall_after
        if brief:
            return data
        data.update({
            "request": request,
            "llm": record.get("llm"),
            "baseline": record.get("baseline"),
            "changed_files": record.get("changed_files") or [],
            "failure_detail": record.get("failure_detail"),
            "completion_summary": record.get("completion_summary"),
            "result": record.get("result"),
            "event_count": record.get("event_count", 0),
        })
        return data

    # -- worker ----------------------------------------------------------
    def _mutate(self, task_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self.store.load(task_id)
            record.update(fields)
            record["updated_at"] = _now()
            self.store.save(record)
            return record

    def _worker(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            done = threading.Event()
            with self._lock:
                self._task_threads[task_id] = threading.current_thread()
                self._task_done_events[task_id] = done
            try:
                self._execute(task_id)
            except Exception:  # never let one task kill the worker thread
                logger.exception("local agent task worker failed for %s", task_id)
                with contextlib.suppress(Exception):
                    self._mutate(task_id, {"status": "failed", "failure_reason": "worker_error",
                                           "finished_at": _now()})
            finally:
                with self._lock:
                    self._cancel_events.pop(task_id, None)
                    self._task_threads.pop(task_id, None)
                    self._task_done_events.pop(task_id, None)
                    done.set()

    def _execute(self, task_id: str) -> None:
        with self._lock:
            record = self.store.load(task_id)
            if record.get("status") != "queued" or record.get("executor") == "aoitalk_agent":
                return
            if record.get("cancel_requested"):
                self._mutate(task_id, {"status": "cancelled", "failure_reason": "cancelled", "finished_at": _now()})
                return
            cancel_event = threading.Event()
            self._cancel_events[task_id] = cancel_event
        spec = LocalAgentTaskSpec(**{**record["request"],
                                     "constraints": tuple(record["request"].get("constraints") or ()),
                                     "completion_criteria": tuple(record["request"].get("completion_criteria") or ())})
        redactor = SecretRedactor()
        state_lock = threading.Lock()
        event_count = [int(record.get("event_count") or 0)]
        pending: dict[str, Any] = {}

        def update(fields: Mapping[str, Any]) -> None:
            with state_lock:
                pending.update(fields)
                pending["counters"] = dict(ctx.counters)
                pending["last_progress_at"] = _now()
                self._mutate(task_id, dict(pending))
                pending.clear()

        def emit(kind: str, payload: Mapping[str, Any]) -> None:
            with state_lock:
                event_count[0] += 1
                if event_count[0] <= self.limits.max_events:
                    event = {"ts": round(_now(), 3), "type": kind, **redactor.value(dict(payload), 2000)}
                    self.store.append_event(task_id, event)
                activity = {
                    "command_start": lambda: f"command: {redactor.text(str(payload.get('command')), 160)}",
                    "command_end": lambda: f"command exit={payload.get('exit_code')}",
                    "tool_blocked": lambda: f"blocked: {payload.get('tool')}",
                    "file_changed": lambda: f"{payload.get('operation')} {payload.get('path')}",
                    "llm_response": lambda: f"llm response ({payload.get('tool_calls')} tool calls)",
                    "assistant_note": lambda: "assistant note",
                }.get(kind, lambda: kind)()
                self._mutate(task_id, {"latest_activity": activity, "last_progress_at": _now(),
                                       "counters": dict(ctx.counters), "event_count": event_count[0]})

        ctx = TaskRunContext(task_id=task_id, spec=spec, limits=self.limits, cancel_event=cancel_event,
                             emit=emit, update=update, redactor=redactor, user_id=record.get("owner"))
        ctx.codex_profile_receipt = record.get("codex_profile_binding")
        if record.get("instruction_review") is not None:
            from ..llm.codex_instruction_review import ReviewedHostInstructions
            ctx.instruction_review = ReviewedHostInstructions(
                record["instruction_review"], task_id=task_id, owner=record.get("owner"),
                fingerprint=spec.fingerprint(), boot_id=self.boot_id)
        now = _now()
        self._mutate(task_id, {"status": "running", "started_at": now, "boot_id": self.boot_id,
                               "heartbeat_at": now, "last_progress_at": now, "current_step": "starting",
                               "latest_activity": "starting"})
        emit("status", {"status": "running"})
        alive = threading.Event()

        def heartbeat() -> None:
            while not alive.wait(self.HEARTBEAT_SEC):
                with contextlib.suppress(Exception):
                    with self._lock:
                        current = self.store.load(task_id)
                        current["heartbeat_at"] = _now()
                        self.store.save(current)

        beat = threading.Thread(target=heartbeat, name=f"local-agent-heartbeat-{task_id[:8]}", daemon=True)
        beat.start()
        try:
            try:
                outcome = self.runner.run(ctx)
            except Exception as exc:
                logger.exception("local agent task %s crashed", task_id)
                outcome = {"status": "failed", "failure_reason": "worker_error",
                           "failure_detail": redactor.text(type(exc).__name__, 200)}
            current = self.store.load(task_id)
            if current.get("cancel_reason") == "aoitalk_shutdown" and outcome.get("status") == "cancelled":
                outcome = {**outcome, "status": "failed", "failure_reason": "interrupted_by_shutdown"}
            final = {
                "status": outcome.get("status", "failed"),
                "failure_reason": outcome.get("failure_reason"),
                "failure_detail": outcome.get("failure_detail"),
                "completion_summary": outcome.get("completion_summary"),
                "summary_text": outcome.get("summary_text"),
                "result": outcome.get("result"),
                "changed_files": outcome.get("changed_files") or [],
                "counters": dict(ctx.counters),
                "finished_at": _now(),
                "current_step": "finished",
                "latest_activity": outcome.get("status", "failed"),
            }
            self._mutate(task_id, final)
            emit("status", {"status": final["status"], "reason": final["failure_reason"]})
        finally:
            alive.set()


_MANAGER: LocalAgentTaskManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_local_agent_task_manager(
    config: Any,
    *,
    codex_preflight: Callable[[TaskRunContext], None] | None = None,
) -> LocalAgentTaskManager:
    """Return the process manager with trusted route hooks wired once.

    The HTTP route must not manufacture a runner without the server-owned
    Codex managed-configuration preflight.  Keeping the optional callback
    here also preserves the existing test/embedding API for local-only
    callers, while allowing a server restart to attach the hook to an already
    cached manager before it starts accepting work.
    """
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            runner = LocalAgentTaskRunner(config, codex_preflight=codex_preflight)
            _MANAGER = LocalAgentTaskManager(config=config, runner=runner,
                host_instruction_owner_id=os.environ.get("AOITALK_CODEX_HOST_INSTRUCTIONS_OWNER_ID"))
        elif codex_preflight is not None and _MANAGER.runner.codex_preflight is None:
            _MANAGER.runner.codex_preflight = codex_preflight
        return _MANAGER


def local_agent_tasks_enabled(config: Any) -> bool:
    from ..features import Features

    if Features.is_enterprise():
        return False
    value = _cfg(config, "local_agent_tasks.enabled", True)
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}
