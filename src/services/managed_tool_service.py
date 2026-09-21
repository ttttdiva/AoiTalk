"""Durable agent-created managed-tool lineage and App promotion service.

This service is intentionally server-controlled:

* the server mints lineage IDs and chooses the only writable source path;
* callers submit script *content*, never an arbitrary path to enroll;
* execution evidence references existing ``AgentRun`` identities and stores no
  duplicate stdout/result telemetry;
* promotion uses the existing ``AppService`` workspace/Manifest/README/Git
  infrastructure and is idempotent for one lineage.

The execution adapter is a small interface so Personal can use the bounded
local subprocess implementation today while Enterprise can later bind a
trusted sandbox without changing lineage or promotion semantics.
"""

from __future__ import annotations

import asyncio
import copy
from collections import deque
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import UUID, uuid4

import yaml
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    AgentRun,
    App,
    ManagedToolLineage,
    ManagedToolObservation,
    ManagedToolPromotionAudit,
    ManagedToolRevision,
    Project,
    ProjectApp,
)
from .app_git_service import AppGitError, AppGitService
from .app_operation_lock import app_operation_lock, project_operation_lock
from .app_manifest_service import (
    load_app_manifest,
    sync_manifest_targets_unlocked,
    validate_manifest_workspace,
)
from .app_service import AppAccessError, AppService, _force_remove_tree
from .app_storage import (
    AppWorkspaceJournal,
    ensure_app_instance,
    get_app_instance_path,
    get_app_workspace_path,
    get_workspaces_root,
    resolve_workspace_file,
)

logger = logging.getLogger(__name__)


class ManagedToolError(RuntimeError):
    """Base error raised by managed-tool lifecycle operations."""


class ManagedToolNotFoundError(ManagedToolError):
    """The requested lineage/revision does not exist."""


class ManagedToolAuthorizationError(PermissionError, ManagedToolError):
    """The authenticated principal cannot access the lineage/project."""


class ManagedToolPathError(ValueError, ManagedToolError):
    """A managed path or enrollment payload is unsafe."""


ALLOWED_MANAGED_TOOL_RUNTIMES = frozenset({"python", "powershell", "shell", "node"})
RUNTIME_EXTENSIONS = {
    "python": ".py",
    "powershell": ".ps1",
    "shell": ".sh",
    "node": ".js",
}
RUNTIME_ALIASES = {
    "py": "python",
    "python3": "python",
    "ps": "powershell",
    "pwsh": "powershell",
    "bash": "shell",
    "sh": "shell",
    "javascript": "node",
    "js": "node",
}
_FORBIDDEN_METADATA_KEYS = frozenset(
    {"stdout", "stderr", "result", "output", "raw_result", "response", "telemetry"}
)
_EXECUTION_EVIDENCE_TOKEN = object()


def _strict_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ManagedToolPathError("managed-tool boolean policy value is invalid")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & flag
    )


def _reject_reparse_components(path: Path, *, stop: Path) -> None:
    current = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(stop))
    components: list[Path] = []
    while True:
        components.append(current)
        if os.path.normcase(str(current)) == os.path.normcase(str(boundary)):
            break
        if current == current.parent:
            raise ManagedToolPathError("managed path escaped its trusted root")
        current = current.parent
    for component in reversed(components):
        if _is_link_or_reparse(component):
            raise ManagedToolPathError(
                f"managed path contains a symbolic link/reparse point: {component}"
            )


def normalize_managed_tool_runtime(value: str) -> str:
    runtime = str(value or "").strip().casefold()
    runtime = RUNTIME_ALIASES.get(runtime, runtime)
    if runtime not in ALLOWED_MANAGED_TOOL_RUNTIMES:
        raise ManagedToolPathError(
            f"runtime must be one of {', '.join(sorted(ALLOWED_MANAGED_TOOL_RUNTIMES))}"
        )
    return runtime


def _as_uuid(value: UUID | str | None, label: str, *, required: bool = False) -> UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ManagedToolAuthorizationError(f"{label} is required")
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ManagedToolAuthorizationError(f"{label} is not a valid UUID") from exc


def _user_uuid(value: UUID | str) -> UUID:
    result = _as_uuid(value, "user_id", required=True)
    assert result is not None
    return result


def _safe_text(value: Any, *, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise ManagedToolPathError(f"{label} must be a string")
    text = value
    if not text.strip():
        raise ManagedToolPathError(f"{label} must not be empty")
    if len(text.encode("utf-8")) > limit:
        raise ManagedToolPathError(f"{label} exceeds the maximum size")
    return text


def _slug(value: str, fallback: str = "managed-tool") -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    return (result[:72] or fallback).strip("-") or fallback


def _redact_semantic_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep small semantic facts while excluding execution payload telemetry."""

    if not isinstance(value, Mapping):
        return {}

    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 4:
            return None
        if isinstance(item, Mapping):
            return {
                str(key): clean(sub, depth + 1)
                for key, sub in item.items()
                if str(key).casefold() not in _FORBIDDEN_METADATA_KEYS
            }
        if isinstance(item, (list, tuple)):
            return [clean(sub, depth + 1) for sub in item[:32]]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)[:500]

    result = clean(value)
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class ManagedToolPromotionPolicy:
    """Explicit, testable promotion policy.

    The defaults require two successful observations from two independent root
    AgentRuns.  Child runs/retries under one root therefore never satisfy the
    default policy by themselves.
    """

    minimum_successful_runs: int = 2
    minimum_distinct_root_runs: int = 2
    auto_promote: bool = True
    max_script_bytes: int = 1_000_000
    execution_timeout_seconds: float = 30.0
    max_output_bytes: int = 32_768
    allowed_runtimes: frozenset[str] = field(
        default_factory=lambda: ALLOWED_MANAGED_TOOL_RUNTIMES
    )

    def __post_init__(self) -> None:
        if isinstance(self.minimum_successful_runs, bool) or isinstance(
            self.minimum_distinct_root_runs,
            bool,
        ):
            raise ValueError("managed-tool promotion thresholds must be integers")
        successful = int(self.minimum_successful_runs)
        roots = int(self.minimum_distinct_root_runs)
        if successful < 2 or roots < 2:
            raise ValueError(
                "managed-tool promotion requires at least two successful "
                "AgentRuns and two distinct root runs"
            )
        timeout = float(self.execution_timeout_seconds)
        if not math.isfinite(timeout) or not (0.1 <= timeout <= 86_400):
            raise ValueError(
                "execution_timeout_seconds must be finite and between 0.1 and 86400"
            )
        max_bytes = int(self.max_script_bytes)
        if not (1 <= max_bytes <= 50 * 1024 * 1024):
            raise ValueError("max_script_bytes must be between 1 and 52428800")
        max_output = int(self.max_output_bytes)
        if not (1 <= max_output <= 64 * 1024 * 1024):
            raise ValueError("max_output_bytes must be between 1 and 67108864")
        runtimes = frozenset(normalize_managed_tool_runtime(item) for item in self.allowed_runtimes)
        if not runtimes:
            raise ValueError("allowed_runtimes must not be empty")
        object.__setattr__(self, "minimum_successful_runs", successful)
        object.__setattr__(self, "minimum_distinct_root_runs", roots)
        object.__setattr__(self, "execution_timeout_seconds", timeout)
        object.__setattr__(self, "max_script_bytes", max_bytes)
        object.__setattr__(self, "max_output_bytes", max_output)
        object.__setattr__(self, "allowed_runtimes", runtimes)
        object.__setattr__(
            self,
            "auto_promote",
            _strict_bool(self.auto_promote, default=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_successful_runs": self.minimum_successful_runs,
            "minimum_distinct_root_runs": self.minimum_distinct_root_runs,
            "auto_promote": self.auto_promote,
            "max_script_bytes": self.max_script_bytes,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "allowed_runtimes": sorted(self.allowed_runtimes),
        }

    @classmethod
    def from_config(cls, config: Any | None = None) -> "ManagedToolPromotionPolicy":
        """Read policy values from dict/Config-like objects with safe defaults."""

        raw: Mapping[str, Any] = {}
        if isinstance(config, Mapping):
            # Accept both flat config and the canonical nested form:
            # ``apps: {managed_tool_promotion: {...}}``.
            paths = (
                ("managed_tools",),
                ("managed_tool_promotion",),
                ("apps", "managed_tools"),
                ("apps", "managed_tool_promotion"),
            )
            for path in paths:
                candidate: Any = config
                for key in path:
                    if not isinstance(candidate, Mapping) or key not in candidate:
                        candidate = None
                        break
                    candidate = candidate[key]
                if isinstance(candidate, Mapping):
                    raw = candidate
                    break
            if not raw and isinstance(config, Mapping):
                # A flat policy mapping is also a valid input to this parser.
                raw = config
        elif config is not None and callable(getattr(config, "get", None)):
            for key in (
                "apps.managed_tool_promotion",
                "apps.managed_tools",
                "managed_tools",
                "managed_tool_promotion",
            ):
                try:
                    candidate = config.get(key)
                except Exception:
                    candidate = None
                if isinstance(candidate, Mapping):
                    raw = candidate
                    break

        def pick(*keys: str, default: Any = None) -> Any:
            for key in keys:
                if key in raw and raw[key] is not None:
                    return raw[key]
            return default

        runtimes = pick("allowed_runtimes", "runtime_allowlist", default=ALLOWED_MANAGED_TOOL_RUNTIMES)
        if isinstance(runtimes, str):
            runtimes = [part.strip() for part in runtimes.split(",") if part.strip()]
        if runtimes is None:
            runtimes = ALLOWED_MANAGED_TOOL_RUNTIMES
        if not isinstance(runtimes, (list, tuple, set, frozenset)) or not runtimes:
            raise ManagedToolPathError(
                "managed-tool allowed_runtimes must be a non-empty collection"
            )
        runtime_set = frozenset(
            normalize_managed_tool_runtime(item) for item in runtimes
        )
        return cls(
            minimum_successful_runs=int(
                pick(
                    "minimum_successful_runs",
                    "min_successful_runs",
                    "minimum_successful_independent_runs",
                    "threshold",
                    default=2,
                )
            ),
            minimum_distinct_root_runs=int(
                pick(
                    "minimum_distinct_root_runs",
                    "min_distinct_root_runs",
                    "minimum_independent_roots",
                    default=2,
                )
            ),
            auto_promote=_strict_bool(
                pick("auto_promote", "automatic_promotion", default=True),
                default=True,
            ),
            max_script_bytes=int(pick("max_script_bytes", default=1_000_000)),
            execution_timeout_seconds=float(pick("execution_timeout_seconds", "timeout_seconds", default=30.0)),
            max_output_bytes=int(pick("max_output_bytes", default=32_768)),
            allowed_runtimes=runtime_set,
        )


@dataclass(frozen=True)
class ManagedToolExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": bool(self.success),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


class ManagedToolExecutionAdapter(ABC):
    """Adapter boundary for Personal subprocess or Enterprise sandbox execution."""

    @abstractmethod
    async def execute(
        self,
        *,
        path: Path,
        runtime: str,
        content: bytes | None = None,
        input_json: Mapping[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> ManagedToolExecutionResult:
        raise NotImplementedError


class SubprocessManagedToolExecutionAdapter(ManagedToolExecutionAdapter):
    """Bounded local execution with no shell interpolation or host env leakage."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        max_output_bytes: int = 32_768,
    ) -> None:
        self._environment = dict(environment or {})
        self._max_output_bytes = max(1, min(int(max_output_bytes), 64 * 1024 * 1024))

    @staticmethod
    def _argv(runtime: str, path: Path) -> list[str]:
        if runtime == "python":
            return [sys.executable, str(path)]
        if runtime == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable is None:
                raise ManagedToolError("PowerShell runtime is unavailable")
            return [executable, "-NoProfile", "-NonInteractive", "-File", str(path)]
        if runtime == "shell":
            executable = shutil.which("bash") or shutil.which("sh")
            if executable is None:
                raise ManagedToolError("shell runtime is unavailable")
            return [executable, str(path)]
        if runtime == "node":
            executable = shutil.which("node")
            if executable is None:
                raise ManagedToolError("node runtime is unavailable")
            return [executable, str(path)]
        raise ManagedToolPathError(f"unsupported runtime: {runtime}")

    async def execute(
        self,
        *,
        path: Path,
        runtime: str,
        content: bytes | None = None,
        input_json: Mapping[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> ManagedToolExecutionResult:
        snapshot_path: Path | None = None
        execution_path = path
        if content is not None:
            fd, temporary = tempfile.mkstemp(
                prefix=".managed-exec-",
                suffix=path.suffix,
                dir=str(path.parent),
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(bytes(content))
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)
                raise
            snapshot_path = Path(temporary)
            execution_path = snapshot_path
        argv = self._argv(runtime, execution_path)
        # Keep the environment minimal while retaining platform runtime
        # essentials (notably SystemRoot is required for Python's RNG on
        # Windows).  Credentials and arbitrary host variables are excluded.
        env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "SYSTEMROOT",
                "SystemRoot",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LANG",
                "LC_ALL",
            )
            if key in os.environ
        }
        env["PYTHONUNBUFFERED"] = "1"
        env.update({str(key): str(value) for key, value in self._environment.items()})
        payload = json.dumps(dict(input_json or {}), ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(execution_path.parent),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Keep descendants in an isolated process group so timeout cleanup
                # can terminate the complete tree rather than only the direct
                # interpreter process.
                start_new_session=(os.name != "nt"),
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
            )
        except BaseException:
            if snapshot_path is not None:
                with suppress(FileNotFoundError):
                    snapshot_path.unlink()
            raise
        timed_out = False
        capture: deque[tuple[bool, bytes]] = deque()
        captured_size = 0

        def append_output(data: bytes, *, is_stderr: bool) -> None:
            nonlocal captured_size
            if len(data) >= self._max_output_bytes:
                capture.clear()
                data = data[-self._max_output_bytes :]
                capture.append((is_stderr, data))
                captured_size = len(data)
                return
            capture.append((is_stderr, data))
            captured_size += len(data)
            while captured_size > self._max_output_bytes and capture:
                stream_kind, oldest = capture.popleft()
                overflow = captured_size - self._max_output_bytes
                if len(oldest) > overflow:
                    oldest = oldest[overflow:]
                    capture.appendleft((stream_kind, oldest))
                    captured_size -= overflow
                    break
                captured_size -= len(oldest)

        async def drain(reader: asyncio.StreamReader | None, *, is_stderr: bool) -> None:
            if reader is None:
                return
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    return
                append_output(chunk, is_stderr=is_stderr)

        stdout_task = asyncio.create_task(drain(process.stdout, is_stderr=False))
        stderr_task = asyncio.create_task(drain(process.stderr, is_stderr=True))
        try:
            if process.stdin is not None:
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate_tree(process)
            await process.wait()
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout = b"".join(data for is_stderr, data in capture if not is_stderr)
        stderr = b"".join(data for is_stderr, data in capture if is_stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = process.returncode
        try:
            return ManagedToolExecutionResult(
                success=not timed_out and exit_code == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=exit_code,
                duration_ms=duration_ms,
                timed_out=timed_out,
            )
        finally:
            if snapshot_path is not None:
                with suppress(FileNotFoundError):
                    snapshot_path.unlink()

    @staticmethod
    async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
        """Terminate process and descendants after a bounded timeout."""
        if process.returncode is not None:
            return
        if os.name != "nt":
            try:
                import signal

                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.sleep(0.05)
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            return
        # ``taskkill /T`` is the Windows equivalent of a process-group kill.
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=2.0)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass


class ManagedToolService:
    """Lifecycle, evidence and automatic promotion operations."""

    _lineage_locks: dict[str, asyncio.Lock] = {}
    _lineage_locks_guard = threading.Lock()

    def __init__(
        self,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        policy: ManagedToolPromotionPolicy | Mapping[str, Any] | None = None,
        executor: ManagedToolExecutionAdapter | Callable[..., Any] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.policy = (
            policy
            if isinstance(policy, ManagedToolPromotionPolicy)
            else ManagedToolPromotionPolicy.from_config(policy)
        )
        if executor is None:
            # A plain host subprocess is Personal-only. Enterprise callers
            # must inject the trusted sandbox adapter supplied by the harness
            # integration; silently falling back would create an authority
            # escalation path.
            try:
                from ..features import Features

                if Features.is_enterprise():
                    raise ManagedToolError(
                        "Enterprise requires an injected trusted managed-tool execution adapter"
                    )
            except ImportError:  # pragma: no cover - minimal test environments
                pass
            self.executor = SubprocessManagedToolExecutionAdapter(
                max_output_bytes=self.policy.max_output_bytes
            )
        else:
            self.executor = executor

    # ------------------------------------------------------------------
    # Trusted path and identity helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _require_owned_transaction(session: AsyncSession) -> None:
        """Prevent service-owned commits from flushing unrelated caller work."""

        pending = tuple(getattr(session, "new", ()) or ())
        dirty = tuple(getattr(session, "dirty", ()) or ())
        deleted = tuple(getattr(session, "deleted", ()) or ())
        if pending or dirty or deleted:
            raise ManagedToolError(
                "managed-tool operations require a clean, operation-owned DB session"
            )

    def _managed_user_root(self, user_id: UUID) -> Path:
        configured_root = self.workspace_root or os.environ.get(
            "AOITALK_WORKSPACES_DIR",
            "./workspaces",
        )
        lexical_workspace = Path(str(configured_root)).expanduser()
        if not lexical_workspace.is_absolute():
            lexical_workspace = Path.cwd() / lexical_workspace
        lexical_workspace = Path(os.path.abspath(lexical_workspace))
        _reject_reparse_components(
            lexical_workspace,
            stop=Path(lexical_workspace.anchor),
        )
        if lexical_workspace.exists() and _is_link_or_reparse(lexical_workspace):
            raise ManagedToolPathError(
                "managed tool workspace root cannot be a link/reparse point"
            )
        workspace = get_workspaces_root(self.workspace_root)
        users_root = workspace / "_users"
        root = users_root / f"user_{user_id}" / "managed_tools"
        for component in (users_root, root.parent, root):
            if component.exists() and _is_link_or_reparse(component):
                raise ManagedToolPathError(
                    "managed tool workspace contains a symbolic link/reparse point"
                )
            component.mkdir(parents=True, exist_ok=True)
            if _is_link_or_reparse(component):
                raise ManagedToolPathError(
                    "managed tool workspace contains a symbolic link/reparse point"
                )
        _reject_reparse_components(root, stop=users_root)
        resolved = root.resolve(strict=True)
        try:
            resolved.relative_to(users_root.resolve(strict=True))
        except ValueError as exc:
            raise ManagedToolPathError(
                "managed tool workspace escaped its canonical root"
            ) from exc
        return resolved

    def _canonical_path(self, *, user_id: UUID, lineage_id: UUID, runtime: str) -> tuple[Path, str]:
        root = self._managed_user_root(user_id)
        entrypoint = f"tool{RUNTIME_EXTENSIONS[runtime]}"
        directory = root / f"lineage_{lineage_id}"
        directory.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(directory, stop=root)
        path = Path(os.path.abspath(directory / entrypoint))
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ManagedToolPathError("managed tool path escaped user workspace") from exc
        return path, f"lineage_{lineage_id}/{entrypoint}"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise ManagedToolPathError("managed tool source path cannot be a symbolic link")
        fd, temporary = tempfile.mkstemp(prefix=".managed-tool-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _validated_source(
        self,
        lineage: ManagedToolLineage,
        *,
        owner: UUID,
    ) -> tuple[Path, bytes]:
        """Validate the exact server-minted path and persisted revision hash."""

        expected, expected_entrypoint = self._canonical_path(
            user_id=owner,
            lineage_id=lineage.id,
            runtime=lineage.runtime,
        )
        persisted = Path(os.path.abspath(str(lineage.canonical_path)))
        if os.path.normcase(str(persisted)) != os.path.normcase(str(expected)):
            raise ManagedToolPathError(
                "persisted managed source path does not match its server-minted lineage path"
            )
        if str(lineage.entrypoint) != expected_entrypoint:
            raise ManagedToolPathError("persisted managed source entrypoint is invalid")
        root = self._managed_user_root(owner)
        _reject_reparse_components(persisted, stop=root)
        if not persisted.is_file() or _is_link_or_reparse(persisted):
            raise ManagedToolNotFoundError("managed tool source file is unavailable")
        try:
            payload = persisted.read_bytes()
        except OSError as exc:
            raise ManagedToolNotFoundError(
                "managed tool source file cannot be read"
            ) from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != str(lineage.current_sha256 or ""):
            raise ManagedToolPathError(
                "managed tool source content does not match its durable revision SHA"
            )
        return persisted, payload

    async def _validated_current_revision_source(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
        *,
        owner: UUID,
    ) -> tuple[Path, bytes]:
        path, payload = self._validated_source(lineage, owner=owner)
        if lineage.current_revision_id is None:
            raise ManagedToolNotFoundError("managed tool has no current revision")
        revision = await session.get(
            ManagedToolRevision,
            lineage.current_revision_id,
        )
        if (
            revision is None
            or revision.lineage_id != lineage.id
            or revision.sha256 != lineage.current_sha256
            or revision.runtime != lineage.runtime
            or revision.entrypoint != lineage.entrypoint
        ):
            raise ManagedToolPathError(
                "managed tool current revision metadata is inconsistent"
            )
        return path, payload

    async def _lineage_lock(self, lineage_id: UUID):
        key = str(lineage_id)
        # ``asyncio.Lock`` objects are loop-bound only after waiting.  Keep a
        # tiny per-process registry and recreate stale-loop locks when needed.
        with self._lineage_locks_guard:
            lock = self._lineage_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._lineage_locks[key] = lock
        return lock

    async def _resolve_run_ids(
        self,
        session: AsyncSession,
        *,
        owner_user_id: UUID | None = None,
        agent_run_id: UUID | str | None,
        root_run_id: UUID | str | None,
    ) -> tuple[UUID | None, UUID | None]:
        agent_id = _as_uuid(agent_run_id, "agent_run_id")
        root_id = _as_uuid(root_run_id, "root_run_id")
        if agent_id is not None:
            getter = getattr(session, "get", None)
            if not callable(getter):
                # Evidence must be anchored to a durable AgentRun row.  A
                # UUID-looking payload alone is never a promotion identity.
                return None, None
            try:
                run = await getter(AgentRun, agent_id)
            except Exception:
                run = None
            if run is None:
                return None, None
            if owner_user_id is not None and str(getattr(run, "user_id", "")) != str(owner_user_id):
                # An AgentRun belonging to another principal cannot become
                # evidence for this user's managed-tool lineage.
                return None, None
            if str(getattr(run, "status", "") or "").casefold() not in {
                "running",
                "succeeded",
            }:
                return None, None
            durable_root = getattr(run, "root_run_id", None) or agent_id
            # Ignore any caller-provided root override; the durable AgentRun
            # hierarchy is authoritative and model payloads cannot forge it.
            root_id = _as_uuid(durable_root, "root_run_id")
            if root_id is None:
                return None, None
            if root_id != agent_id:
                try:
                    root_run = await getter(AgentRun, root_id)
                except Exception:
                    root_run = None
                if root_run is None or (
                    owner_user_id is not None
                    and str(getattr(root_run, "user_id", "")) != str(owner_user_id)
                ):
                    return None, None
                if str(getattr(root_run, "status", "") or "").casefold() not in {
                    "running",
                    "succeeded",
                }:
                    return None, None
            return agent_id, root_id
        # A standalone root UUID is not sufficient evidence either: it has no
        # durable run row to prove ownership or independence.
        return None, None

    async def _get_lineage(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str | None = None,
        for_update: bool = False,
    ) -> ManagedToolLineage:
        parsed = _as_uuid(lineage_id, "lineage_id", required=True)
        statement = select(ManagedToolLineage).where(ManagedToolLineage.id == parsed)
        if for_update:
            statement = statement.with_for_update()
        lineage = await session.scalar(statement)
        if lineage is None:
            raise ManagedToolNotFoundError("managed tool lineage was not found")
        if user_id is not None and lineage.owner_user_id != _user_uuid(user_id):
            raise ManagedToolAuthorizationError("managed tool belongs to another user")
        return lineage

    async def _authorize_project(
        self,
        session: AsyncSession,
        *,
        project_id: UUID | str,
        user_id: UUID,
        require_write: bool = True,
    ) -> UUID:
        project_uuid = _as_uuid(project_id, "project_id", required=True)
        assert project_uuid is not None
        project = await session.scalar(
            select(Project.id)
            .where(Project.id == project_uuid, Project.deleted_at.is_(None))
            .with_for_update()
        )
        if project is None:
            raise ManagedToolAuthorizationError("Project was not found")
        # Re-evaluate ACLs only after the canonical Project row is locked.
        # Every managed mutation also holds the cross-process Project operation
        # lock, keeping the check adjacent to the binding/publication change.
        app_service = AppService(workspace_root=self.workspace_root)
        if not await app_service.project_access(session, project_id=project_uuid, user_id=user_id):
            raise ManagedToolAuthorizationError("Project access is required")
        if require_write and not await app_service.project_write_access(session, project_id=project_uuid, user_id=user_id):
            raise ManagedToolAuthorizationError("Project write access is required for managed-tool promotion")
        return project_uuid

    async def _authorize_lineage_project_context(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
        *,
        context_project_id: UUID | str | None,
        user_id: UUID,
        require_write: bool,
    ) -> None:
        if lineage.project_id is None:
            return
        trusted_project = _as_uuid(context_project_id, "context_project_id")
        if trusted_project is None or trusted_project != lineage.project_id:
            raise ManagedToolAuthorizationError(
                "managed tool requires its server-selected Project context"
            )
        await self._authorize_project(
            session,
            project_id=lineage.project_id,
            user_id=user_id,
            require_write=require_write,
        )

    # ------------------------------------------------------------------
    # Creation/update/list/observation lifecycle
    # ------------------------------------------------------------------
    async def create_tool(
        self,
        session: AsyncSession,
        *,
        user_id: UUID | str,
        name: str,
        content: str,
        runtime: str = "python",
        description: str = "",
        project_id: UUID | str | None = None,
        agent_run_id: UUID | str | None = None,
        root_run_id: UUID | str | None = None,
        semantic_key: str | None = None,
        source_path: str | os.PathLike[str] | None = None,
        policy: ManagedToolPromotionPolicy | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> ManagedToolLineage:
        """Create an agent-generated source using a server-minted workspace path.

        ``source_path`` and path-like compatibility kwargs are rejected rather
        than interpreted; this is the no-arbitrary-file enrollment boundary.
        """

        if source_path is not None and str(source_path).strip():
            raise ManagedToolPathError("existing/user/repository files cannot be enrolled")
        for key in ("path", "canonical_path", "file_path", "source_file"):
            if kwargs.get(key):
                raise ManagedToolPathError("managed tool source path is server controlled")
        self._require_owned_transaction(session)
        owner = _user_uuid(user_id)
        selected_policy = policy if isinstance(policy, ManagedToolPromotionPolicy) else ManagedToolPromotionPolicy.from_config(policy or self.policy.to_dict())
        selected_runtime = normalize_managed_tool_runtime(runtime)
        if selected_runtime not in selected_policy.allowed_runtimes:
            raise ManagedToolPathError("runtime is not allowed by managed-tool policy")
        script = _safe_text(content, limit=selected_policy.max_script_bytes, label="content")
        clean_name = _safe_text(str(name or "").strip(), limit=255, label="name")
        project_uuid = _as_uuid(project_id, "project_id")
        if project_uuid is not None:
            # Project lock/row lock is held across source creation so a later
            # promotion cannot race a binding or Project deletion.
            lock = project_operation_lock(project_uuid, workspace_root=self.workspace_root)
            await lock.acquire()
        else:
            lock = None
        canonical: Path | None = None
        try:
            if project_uuid is not None:
                await self._authorize_project(session, project_id=project_uuid, user_id=owner)
            lineage_id = uuid4()
            canonical, entrypoint = self._canonical_path(user_id=owner, lineage_id=lineage_id, runtime=selected_runtime)
            self._atomic_write(canonical, script)
            agent_id, root_id = await self._resolve_run_ids(session, owner_user_id=owner, agent_run_id=agent_run_id, root_run_id=root_run_id)
            digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
            lineage = ManagedToolLineage(
                id=lineage_id,
                owner_user_id=owner,
                project_id=project_uuid,
                name=clean_name,
                description=(str(description or "").strip() or None),
                runtime=selected_runtime,
                canonical_path=str(canonical),
                entrypoint=entrypoint,
                source_kind="agent_generated",
                source_agent_run_id=agent_id,
                source_root_run_id=root_id,
                current_sha256=digest,
                policy_snapshot=selected_policy.to_dict(),
                discovery_json={
                    "created": True,
                    "semantic_key": str(semantic_key or "").strip() or None,
                    "successful_runs": 0,
                    "distinct_root_runs": 0,
                },
                status="active",
            )
            revision = ManagedToolRevision(
                id=uuid4(),
                lineage_id=lineage.id,
                sha256=digest,
                runtime=selected_runtime,
                entrypoint=entrypoint,
                agent_run_id=agent_id,
                root_run_id=root_id,
                metadata_json={
                    "source": "agent_generated",
                    "line_count": script.count("\n") + 1,
                    "byte_size": len(script.encode("utf-8")),
                    "semantic_key": str(semantic_key or "").strip() or None,
                },
            )
            session.add(lineage)
            session.add(revision)
            await session.flush()
            lineage.current_revision_id = revision.id
            await session.flush()
            await session.commit()
            return lineage
        except BaseException:
            await session.rollback()
            if canonical is not None:
                lineage_dir = canonical.parent
                try:
                    _reject_reparse_components(
                        lineage_dir,
                        stop=self._managed_user_root(owner),
                    )
                    shutil.rmtree(lineage_dir, ignore_errors=True)
                except Exception:
                    logger.exception(
                        "managed tool create rollback cleanup failed: %s",
                        lineage_dir,
                    )
            raise
        finally:
            if lock is not None:
                lock.release()

    async def update_tool(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str,
        content: str,
        runtime: str | None = None,
        description: str | None = None,
        context_project_id: UUID | str | None = None,
        agent_run_id: UUID | str | None = None,
        root_run_id: UUID | str | None = None,
        semantic_key: str | None = None,
        source_path: str | os.PathLike[str] | None = None,
        policy: ManagedToolPromotionPolicy | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> ManagedToolLineage:
        if source_path is not None and str(source_path).strip():
            raise ManagedToolPathError("managed tool source path is server controlled")
        for key in ("path", "canonical_path", "file_path", "source_file"):
            if kwargs.get(key):
                raise ManagedToolPathError("managed tool source path is server controlled")
        self._require_owned_transaction(session)
        owner = _user_uuid(user_id)
        lineage = await self._get_lineage(session, lineage_id, user_id=owner)
        await self._authorize_lineage_project_context(
            session,
            lineage,
            context_project_id=context_project_id,
            user_id=owner,
            require_write=True,
        )
        selected_policy = policy if isinstance(policy, ManagedToolPromotionPolicy) else ManagedToolPromotionPolicy.from_config(policy or lineage.policy_snapshot or self.policy.to_dict())
        selected_runtime = normalize_managed_tool_runtime(runtime or lineage.runtime)
        if selected_runtime != lineage.runtime:
            raise ManagedToolPathError("runtime changes would break lineage identity")
        if selected_runtime not in selected_policy.allowed_runtimes:
            raise ManagedToolPathError("runtime is not allowed by managed-tool policy")
        script = _safe_text(content, limit=selected_policy.max_script_bytes, label="content")
        lock = None
        project_uuid = lineage.project_id
        if project_uuid is not None:
            lock = project_operation_lock(project_uuid, workspace_root=self.workspace_root)
            await lock.acquire()
        lineage_lock = await self._lineage_lock(lineage.id)
        try:
            await lineage_lock.acquire()
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        canonical: Path | None = None
        previous_content: bytes | None = None
        try:
            if project_uuid is not None:
                await self._authorize_project(session, project_id=project_uuid, user_id=owner)
            canonical, previous_content = await self._validated_current_revision_source(
                session,
                lineage,
                owner=owner,
            )
            digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
            normalized_description = (
                str(description).strip() or None
                if description is not None
                else lineage.description
            )
            description_unchanged = normalized_description == lineage.description
            if digest == lineage.current_sha256 and description_unchanged:
                lineage.policy_snapshot = selected_policy.to_dict()
                lineage.updated_at = datetime.utcnow()
                await session.commit()
                return lineage
            self._atomic_write(canonical, script)
            agent_id, root_id = await self._resolve_run_ids(session, owner_user_id=owner, agent_run_id=agent_run_id, root_run_id=root_run_id)
            revision = await session.scalar(
                select(ManagedToolRevision).where(
                    ManagedToolRevision.lineage_id == lineage.id,
                    ManagedToolRevision.sha256 == digest,
                ).limit(1)
            )
            if revision is None:
                revision = ManagedToolRevision(
                    id=uuid4(),
                    lineage_id=lineage.id,
                    sha256=digest,
                    runtime=selected_runtime,
                    entrypoint=lineage.entrypoint,
                    agent_run_id=agent_id,
                    root_run_id=root_id,
                    metadata_json={
                        "source": "agent_generated",
                        "line_count": script.count("\n") + 1,
                        "byte_size": len(script.encode("utf-8")),
                        "semantic_key": str(semantic_key or "").strip() or None,
                    },
                )
                session.add(revision)
                await session.flush()
            lineage.current_revision_id = revision.id
            lineage.current_sha256 = digest
            lineage.policy_snapshot = selected_policy.to_dict()
            if description is not None:
                lineage.description = normalized_description
            lineage.updated_at = datetime.utcnow()
            await session.flush()
            if lineage.status == "promoted" and lineage.app_id:
                await self._promote_locked(
                    session,
                    lineage,
                    owner,
                    selected_policy,
                    action="updated",
                    project_lock_held=project_uuid is not None,
                )
            else:
                await session.commit()
            return lineage
        except BaseException:
            await session.rollback()
            if canonical is not None and previous_content is not None:
                try:
                    self._atomic_write(
                        canonical,
                        previous_content.decode("utf-8", errors="strict"),
                    )
                except Exception:
                    logger.exception(
                        "managed tool source rollback failed: lineage_id=%s",
                        lineage.id,
                    )
            raise
        finally:
            lineage_lock.release()
            if lock is not None:
                lock.release()

    async def list_tools(
        self,
        session: AsyncSession,
        *,
        user_id: UUID | str,
        project_id: UUID | str | None = None,
        include_evidence: bool = False,
    ) -> list[dict[str, Any]]:
        owner = _user_uuid(user_id)
        project_uuid = _as_uuid(project_id, "project_id")
        if project_uuid is not None:
            app_service = AppService(workspace_root=self.workspace_root)
            if not await app_service.project_access(session, project_id=project_uuid, user_id=owner):
                raise ManagedToolAuthorizationError("Project access is required")
        statement = select(ManagedToolLineage).where(ManagedToolLineage.owner_user_id == owner)
        if project_uuid is not None:
            statement = statement.where(ManagedToolLineage.project_id == project_uuid)
        rows = list((await session.scalars(statement.order_by(ManagedToolLineage.updated_at.desc()))).all())
        result: list[dict[str, Any]] = []
        for lineage in rows:
            # AsyncSession cannot lazy-load relationships from a synchronous
            # ``to_dict`` call after a flush/commit.  Query optional evidence
            # explicitly instead of triggering MissingGreenlet.
            payload = lineage.to_dict(include_evidence=False)
            if include_evidence:
                payload.update(await self._evidence_rows(session, lineage))
            evidence = await self._promotion_evidence(session, lineage.id)
            payload.update(evidence)
            payload["eligible_for_promotion"] = self._eligible(evidence, self._policy_for(lineage))
            result.append(payload)
        return result

    async def get_tool(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str,
        include_evidence: bool = True,
    ) -> dict[str, Any]:
        lineage = await self._get_lineage(session, lineage_id, user_id=user_id)
        payload = lineage.to_dict(include_evidence=False)
        if include_evidence:
            payload.update(await self._evidence_rows(session, lineage))
        evidence = await self._promotion_evidence(session, lineage.id)
        payload.update(evidence)
        payload["eligible_for_promotion"] = self._eligible(evidence, self._policy_for(lineage))
        return payload

    async def _evidence_rows(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
    ) -> dict[str, Any]:
        revisions = list(
            (
                await session.scalars(
                    select(ManagedToolRevision)
                    .where(ManagedToolRevision.lineage_id == lineage.id)
                    .order_by(ManagedToolRevision.created_at)
                )
            ).all()
        )
        observations = list(
            (
                await session.scalars(
                    select(ManagedToolObservation)
                    .where(ManagedToolObservation.lineage_id == lineage.id)
                    .order_by(ManagedToolObservation.created_at)
                )
            ).all()
        )
        audits = list(
            (
                await session.scalars(
                    select(ManagedToolPromotionAudit)
                    .where(ManagedToolPromotionAudit.lineage_id == lineage.id)
                    .order_by(ManagedToolPromotionAudit.created_at)
                )
            ).all()
        )
        return {
            "revisions": [item.to_dict() for item in revisions],
            "observations": [item.to_dict() for item in observations],
            "promotion_audits": [item.to_dict() for item in audits],
        }

    async def record_observation(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str,
        success: bool,
        agent_run_id: UUID | str | None = None,
        root_run_id: UUID | str | None = None,
        revision_id: UUID | str | None = None,
        observation_kind: str = "execution",
        semantic_key: str | None = None,
        exit_code: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        auto_promote: bool = True,
        _execution_evidence_token: object | None = None,
    ) -> dict[str, Any]:
        owner = _user_uuid(user_id)
        if bool(success) and _execution_evidence_token is not _EXECUTION_EVIDENCE_TOKEN:
            raise ManagedToolAuthorizationError(
                "successful managed-tool evidence must come from server-controlled execution"
            )
        lineage = await self._get_lineage(session, lineage_id, user_id=owner, for_update=True)
        selected_revision = None
        if revision_id is not None:
            selected_revision = await session.scalar(
                select(ManagedToolRevision).where(
                    ManagedToolRevision.id == _as_uuid(revision_id, "revision_id", required=True),
                    ManagedToolRevision.lineage_id == lineage.id,
                ).limit(1)
            )
            if selected_revision is None:
                raise ManagedToolNotFoundError("managed tool revision was not found")
        if selected_revision is None:
            selected_revision = await session.scalar(
                select(ManagedToolRevision).where(ManagedToolRevision.id == lineage.current_revision_id).limit(1)
            )
        if selected_revision is None:
            raise ManagedToolNotFoundError("managed tool has no current revision")
        agent_id, root_id = await self._resolve_run_ids(session, owner_user_id=owner, agent_run_id=agent_run_id, root_run_id=root_run_id)
        observation = ManagedToolObservation(
            id=uuid4(),
            lineage_id=lineage.id,
            revision_id=selected_revision.id,
            agent_run_id=agent_id,
            root_run_id=root_id,
            observation_kind=str(observation_kind or "execution").strip()[:64] or "execution",
            semantic_key=str(semantic_key or "").strip()[:160] or None,
            success=bool(success),
            exit_code=exit_code,
            metadata_json=_redact_semantic_metadata(metadata),
        )
        session.add(observation)
        await session.flush()
        promotion: dict[str, Any] | None = None
        if auto_promote:
            promotion = await self.maybe_promote(
                session,
                lineage.id,
                user_id=owner,
            )
        evidence = await self._promotion_evidence(session, lineage.id)
        return {
            "observation": observation.to_dict(),
            "evidence": evidence,
            "promotion": promotion or {"promoted": False, "reason": "auto_promotion_disabled"},
        }

    async def execute_tool(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str,
        input_json: Mapping[str, Any] | None = None,
        context_project_id: UUID | str | None = None,
        agent_run_id: UUID | str | None = None,
        root_run_id: UUID | str | None = None,
        semantic_key: str | None = None,
        auto_promote: bool = True,
    ) -> dict[str, Any]:
        self._require_owned_transaction(session)
        owner = _user_uuid(user_id)
        lineage = await self._get_lineage(session, lineage_id, user_id=owner)
        await self._authorize_lineage_project_context(
            session,
            lineage,
            context_project_id=context_project_id,
            user_id=owner,
            require_write=False,
        )
        selected_policy = self._policy_for(lineage)
        if lineage.runtime not in selected_policy.allowed_runtimes:
            raise ManagedToolPathError("runtime is not allowed by managed-tool policy")
        path, _payload = await self._validated_current_revision_source(
            session,
            lineage,
            owner=owner,
        )
        executed_revision_id = lineage.current_revision_id
        executed_sha256 = lineage.current_sha256
        result = await self._execute_adapter(
            path=path,
            runtime=lineage.runtime,
            content=_payload,
            input_json=input_json,
            timeout_seconds=selected_policy.execution_timeout_seconds,
        )
        semantic_metadata = {
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "runtime": lineage.runtime,
            "executed_sha256": executed_sha256,
        }
        observed = await self.record_observation(
            session,
            lineage.id,
            user_id=owner,
            success=result.success,
            agent_run_id=agent_run_id,
            root_run_id=root_run_id,
            revision_id=executed_revision_id,
            semantic_key=semantic_key,
            exit_code=result.exit_code,
            metadata=semantic_metadata,
            auto_promote=auto_promote,
            _execution_evidence_token=_EXECUTION_EVIDENCE_TOKEN,
        )
        return {"lineage_id": str(lineage.id), "result": result.to_dict(), **observed}

    async def _execute_adapter(self, **kwargs: Any) -> ManagedToolExecutionResult:
        adapter = self.executor
        method = getattr(adapter, "execute", None)
        if method is None and callable(adapter):
            method = adapter
        if method is None:
            raise ManagedToolError("managed tool execution adapter is invalid")
        value = method(**kwargs)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, ManagedToolExecutionResult):
            return ManagedToolExecutionResult(
                success=(
                    _strict_bool(value.success, default=False)
                    and not _strict_bool(value.timed_out, default=False)
                    and value.exit_code in {0, None}
                ),
                stdout=value.stdout,
                stderr=value.stderr,
                exit_code=value.exit_code,
                duration_ms=value.duration_ms,
                timed_out=_strict_bool(value.timed_out, default=False),
            )
        if isinstance(value, Mapping):
            exit_code = value.get("exit_code")
            timed_out = _strict_bool(value.get("timed_out"), default=False)
            claimed_success = _strict_bool(value.get("success"), default=False)
            return ManagedToolExecutionResult(
                success=claimed_success and not timed_out and exit_code in {0, None},
                stdout=str(value.get("stdout") or ""),
                stderr=str(value.get("stderr") or ""),
                exit_code=exit_code,
                duration_ms=value.get("duration_ms"),
                timed_out=timed_out,
            )
        raise ManagedToolError("execution adapter returned an invalid result")

    # ------------------------------------------------------------------
    # Policy and promotion
    # ------------------------------------------------------------------
    def _policy_for(self, lineage: ManagedToolLineage) -> ManagedToolPromotionPolicy:
        snapshot = lineage.policy_snapshot if isinstance(lineage.policy_snapshot, Mapping) else None
        return ManagedToolPromotionPolicy.from_config(snapshot or self.policy.to_dict())

    async def _promotion_evidence(self, session: AsyncSession, lineage_id: UUID) -> dict[str, Any]:
        observations = list(
            (
                await session.scalars(
                    select(ManagedToolObservation)
                    .where(ManagedToolObservation.lineage_id == lineage_id, ManagedToolObservation.success.is_(True))
                    .order_by(ManagedToolObservation.created_at, ManagedToolObservation.id)
                )
            ).all()
        )
        agent_runs = {str(row.agent_run_id) for row in observations if row.agent_run_id is not None}
        roots = {
            str(row.root_run_id or row.agent_run_id)
            for row in observations
            if row.root_run_id is not None or row.agent_run_id is not None
        }
        return {
            "successful_observation_count": len(observations),
            "successful_agent_run_count": len(agent_runs),
            "distinct_root_run_count": len(roots),
            "successful_agent_run_ids": sorted(agent_runs),
            "distinct_root_run_ids": sorted(roots),
        }

    @staticmethod
    def _eligible(evidence: Mapping[str, Any], policy: ManagedToolPromotionPolicy) -> bool:
        return (
            int(evidence.get("successful_agent_run_count") or 0) >= policy.minimum_successful_runs
            and int(evidence.get("distinct_root_run_count") or 0) >= policy.minimum_distinct_root_runs
        )

    async def maybe_promote(
        self,
        session: AsyncSession,
        lineage_id: UUID | str,
        *,
        user_id: UUID | str,
        policy: ManagedToolPromotionPolicy | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = _user_uuid(user_id)
        parsed = _as_uuid(lineage_id, "lineage_id", required=True)
        assert parsed is not None
        preliminary = await self._get_lineage(
            session,
            parsed,
            user_id=owner,
        )
        project_lock = None
        if preliminary.project_id is not None:
            project_lock = project_operation_lock(
                preliminary.project_id,
                workspace_root=self.workspace_root,
            )
            await project_lock.acquire()
        lock = await self._lineage_lock(parsed)
        try:
            await lock.acquire()
        except BaseException:
            if project_lock is not None:
                project_lock.release()
            raise
        try:
            lineage = await self._get_lineage(
                session,
                parsed,
                user_id=owner,
                for_update=True,
            )
            selected_policy = (
                policy
                if isinstance(policy, ManagedToolPromotionPolicy)
                else ManagedToolPromotionPolicy.from_config(
                    policy
                    or lineage.policy_snapshot
                    or self.policy.to_dict()
                )
            )
            evidence = await self._promotion_evidence(session, lineage.id)
            lineage.policy_snapshot = selected_policy.to_dict()
            lineage.discovery_json = {
                **(
                    lineage.discovery_json
                    if isinstance(lineage.discovery_json, Mapping)
                    else {}
                ),
                "eligible": self._eligible(evidence, selected_policy),
                "last_evaluated_at": datetime.utcnow().isoformat(),
                **evidence,
            }
            await session.flush()
            if not selected_policy.auto_promote:
                return {
                    "promoted": False,
                    "reason": "policy_disabled",
                    "evidence": evidence,
                }
            if not self._eligible(evidence, selected_policy):
                return {
                    "promoted": False,
                    "reason": "threshold_not_reached",
                    "evidence": evidence,
                }
            if (
                lineage.app_id is not None
                and lineage.current_revision_id is not None
            ):
                existing_audit = await session.scalar(
                    select(ManagedToolPromotionAudit.id)
                    .where(
                        ManagedToolPromotionAudit.lineage_id == lineage.id,
                        ManagedToolPromotionAudit.revision_id
                        == lineage.current_revision_id,
                    )
                    .limit(1)
                )
                if existing_audit is not None:
                    await self._validate_existing_promotion(
                        session,
                        lineage,
                        owner,
                    )
                    return {
                        "promoted": False,
                        "reason": "revision_already_promoted",
                        "app_id": str(lineage.app_id),
                        "lineage_id": str(lineage.id),
                        "revision_id": str(lineage.current_revision_id),
                        "evidence": evidence,
                    }
            action = "updated" if lineage.app_id else "promoted"
            await self._promote_locked(
                session,
                lineage,
                owner,
                selected_policy,
                action=action,
                evidence=evidence,
                project_lock_held=project_lock is not None,
            )
            return {
                "promoted": True,
                "action": action,
                "app_id": str(lineage.app_id) if lineage.app_id else None,
                "lineage_id": str(lineage.id),
                "revision_id": (
                    str(lineage.current_revision_id)
                    if lineage.current_revision_id
                    else None
                ),
                "evidence": evidence,
            }
        finally:
            lock.release()
            if project_lock is not None:
                project_lock.release()

    async def _validate_existing_promotion(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
        owner: UUID,
    ) -> None:
        if lineage.app_id is None:
            raise ManagedToolError("promoted managed-tool App is unavailable")
        lock = app_operation_lock(
            lineage.app_id,
            workspace_root=self.workspace_root,
        )
        await lock.acquire()
        try:
            await self._validate_existing_promotion_locked(
                session,
                lineage,
                owner,
            )
        finally:
            lock.release()

    async def _validate_existing_promotion_locked(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
        owner: UUID,
    ) -> None:
        """Fail closed before treating a promoted revision as a no-op."""

        app = await session.scalar(
            select(App).where(App.id == lineage.app_id).limit(1)
        )
        if app is None or app.owner_user_id != owner:
            raise ManagedToolError("promoted managed-tool App is unavailable")
        _source_path, source_bytes = await self._validated_current_revision_source(
            session,
            lineage,
            owner=owner,
        )
        workspace = get_app_workspace_path(
            app.id,
            workspace_root=self.workspace_root,
        )
        entrypoint = f"managed_tool{RUNTIME_EXTENSIONS[lineage.runtime]}"
        promoted_path = resolve_workspace_file(workspace, entrypoint)
        if _is_link_or_reparse(promoted_path) or not promoted_path.is_file():
            raise ManagedToolError(
                "promoted managed-tool entrypoint is unavailable"
            )
        if hashlib.sha256(promoted_path.read_bytes()).hexdigest() != (
            hashlib.sha256(source_bytes).hexdigest()
        ):
            raise ManagedToolError(
                "promoted managed-tool entrypoint differs from the durable revision"
            )
        manifest, _text, _digest = load_app_manifest(workspace)
        target = (manifest.get("targets") or {}).get("managed_tool")
        if not isinstance(target, Mapping):
            raise ManagedToolError(
                "promoted managed-tool target is unavailable"
            )
        if str(target.get("entrypoint") or "") != entrypoint:
            raise ManagedToolError(
                "promoted managed-tool target entrypoint is invalid"
            )
        expected_command = self._manifest_command(lineage.runtime, entrypoint)
        target_command = target.get("run")
        if isinstance(target_command, Mapping):
            target_command = target_command.get("command")
        if (
            str(target.get("runtime") or "") != lineage.runtime
            or str(target.get("surface") or "") != "headless"
            or str(target.get("execution_host") or "") != "server"
            or str(target_command or "") != expected_command
            or list(target.get("capabilities") or []) != []
        ):
            raise ManagedToolError(
                "promoted managed-tool target configuration is invalid"
            )
        lineage_marker = str(
            target.get("x_aoitalk_managed_tool_lineage") or ""
        ).strip()
        if lineage_marker != str(lineage.id):
            raise ManagedToolError(
                "promoted managed-tool target lineage is missing or invalid"
            )
        if AppGitService(
            workspace_root=self.workspace_root
        ).status(app.id).get("dirty"):
            raise ManagedToolError(
                "promoted managed-tool App has uncommitted changes"
            )

    async def _promote_locked(
        self,
        session: AsyncSession,
        lineage: ManagedToolLineage,
        owner: UUID,
        policy: ManagedToolPromotionPolicy,
        *,
        action: str,
        evidence: Mapping[str, Any] | None = None,
        project_lock_held: bool = False,
    ) -> App:
        """Promote/update one lineage under Project/App and cross-store locks."""

        project_lock = None
        project_lock_acquired_here = False
        app_lock = None
        created_new_app = False
        app_workspace: Path | None = None
        app_instance_path: Path | None = None
        app_instance_existed = True
        journal: AppWorkspaceJournal | None = None
        git: AppGitService | None = None
        previous_git_revision: str | None = None
        checkpoint_revision: str | None = None
        app: App | None = None

        if lineage.project_id is not None:
            project_lock = project_operation_lock(
                lineage.project_id,
                workspace_root=self.workspace_root,
            )
            if not project_lock_held:
                # Never use locked() as an ownership test: another task
                # holding the shared lock must make this promotion wait.
                await project_lock.acquire()
                project_lock_acquired_here = True
            try:
                await self._authorize_project(
                    session,
                    project_id=lineage.project_id,
                    user_id=owner,
                )
            except BaseException:
                if project_lock_acquired_here:
                    project_lock.release()
                    project_lock_acquired_here = False
                raise

        try:
            _source_path, source_bytes = await self._validated_current_revision_source(
                session,
                lineage,
                owner=owner,
            )
            try:
                source_text = source_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ManagedToolPathError(
                    "managed tool source is not valid UTF-8"
                ) from exc

            if lineage.app_id is not None:
                app = await session.scalar(
                    select(App).where(App.id == lineage.app_id).limit(1)
                )
                if app is None:
                    raise ManagedToolError(
                        "managed tool promotion points to a missing App"
                    )
                if app.owner_user_id != owner:
                    raise ManagedToolAuthorizationError(
                        "promoted App belongs to another user"
                    )
            else:
                app = await AppService(
                    workspace_root=self.workspace_root
                ).create_app(
                    session,
                    owner_user_id=owner,
                    name=f"Managed Tool - {lineage.name}",
                    slug=(
                        "managed-tool-"
                        f"{str(lineage.id).replace('-', '')[:24]}"
                    ),
                    description=(
                        lineage.description
                        or f"Agent-generated reusable {lineage.runtime} tool"
                    ),
                    origin_project_id=lineage.project_id,
                    visibility="private",
                )
                lineage.app_id = app.id
                created_new_app = True
            if lineage.description is not None:
                app.description = lineage.description

            app_workspace = get_app_workspace_path(
                app.id,
                workspace_root=self.workspace_root,
            )
            app_lock = app_operation_lock(
                app.id,
                workspace_root=self.workspace_root,
            )
            await app_lock.acquire()

            app_entrypoint = f"managed_tool{RUNTIME_EXTENSIONS[lineage.runtime]}"
            destination = resolve_workspace_file(app_workspace, app_entrypoint)
            manifest_path = resolve_workspace_file(
                app_workspace,
                "aoitalk.app.yaml",
            )
            readme_path = resolve_workspace_file(app_workspace, "README.md")
            for protected_path in (
                app_workspace,
                destination,
                manifest_path,
                readme_path,
            ):
                if _is_link_or_reparse(protected_path):
                    raise ManagedToolPathError(
                        "promoted App workspace contains a link/reparse point"
                    )

            existing_manifest, _manifest_text, _manifest_digest = load_app_manifest(
                app_workspace
            )
            manifest = copy.deepcopy(existing_manifest)
            targets = manifest.get("targets")
            if not isinstance(targets, dict):
                raise ManagedToolError("promoted App Manifest targets are invalid")
            existing_managed_target = targets.get("managed_tool")
            if existing_managed_target is not None:
                if not isinstance(existing_managed_target, Mapping):
                    raise ManagedToolError(
                        "existing managed_tool target is not replaceable"
                    )
                existing_entrypoint = str(
                    existing_managed_target.get("entrypoint") or ""
                ).strip()
                existing_lineage = str(
                    existing_managed_target.get(
                        "x_aoitalk_managed_tool_lineage"
                    )
                    or ""
                ).strip()
                if existing_entrypoint and existing_entrypoint != app_entrypoint:
                    raise ManagedToolError(
                        "existing managed_tool target belongs to another entrypoint"
                    )
                if existing_lineage != str(lineage.id):
                    raise ManagedToolError(
                        "existing managed_tool target belongs to another lineage"
                    )
            targets["managed_tool"] = {
                "display_name": lineage.name,
                "description": lineage.description or "",
                "surface": "headless",
                "runtime": lineage.runtime,
                "execution_host": "server",
                "entrypoint": app_entrypoint,
                "run": {
                    "command": self._manifest_command(
                        lineage.runtime,
                        app_entrypoint,
                    )
                },
                "capabilities": [],
                "x_aoitalk_managed_tool_lineage": str(lineage.id),
            }
            manifest["schema_version"] = int(
                manifest.get("schema_version") or 1
            )
            manifest.setdefault("name", app.name)
            manifest.setdefault("description", app.description or "")

            readme_text = (
                readme_path.read_text(encoding="utf-8")
                if readme_path.exists()
                else ""
            )
            marker = f"Managed tool lineage: {lineage.id}"
            if marker not in readme_text:
                readme_text = (
                    readme_text.rstrip()
                    + f"\n\n## Managed tool\n\n{marker}\n\n"
                    f"Runtime: {lineage.runtime}\n\n"
                    f"Entrypoint: {app_entrypoint}\n"
                )

            git = AppGitService(workspace_root=self.workspace_root)
            git_status = git.status(app.id)
            if git_status.get("dirty"):
                raise ManagedToolError(
                    "promoted App workspace has uncommitted changes"
                )
            previous_git_revision = (
                str(git_status.get("revision") or "").strip() or None
            )

            journal = AppWorkspaceJournal(app_workspace)
            journal.stash(
                app_entrypoint,
                "aoitalk.app.yaml",
                "README.md",
            )
            self._atomic_write(destination, source_text)
            manifest_path.write_text(
                yaml.safe_dump(
                    manifest,
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
                newline="\n",
            )
            readme_path.write_text(
                readme_text,
                encoding="utf-8",
                newline="\n",
            )
            validate_manifest_workspace(manifest, app_workspace)

            app_service = AppService(workspace_root=self.workspace_root)
            await app_service.ensure_readme_node(
                session,
                app,
                owner,
                workspace=app_workspace,
            )
            await app_service.sync_readme_to_node(session, app, owner)
            await sync_manifest_targets_unlocked(
                session,
                app,
                app_workspace,
            )

            checkpoint_revision = git.checkpoint(
                app.id,
                f"Managed tool {action}: {lineage.name}",
                actor=str(owner),
            )
            if not checkpoint_revision:
                raise ManagedToolError(
                    "managed tool App Git checkpoint did not produce a revision"
                )

            if lineage.project_id is not None:
                binding = await session.scalar(
                    select(ProjectApp)
                    .where(
                        ProjectApp.project_id == lineage.project_id,
                        ProjectApp.app_id == app.id,
                    )
                    .limit(1)
                )
                if binding is None:
                    session.add(
                        ProjectApp(
                            project_id=lineage.project_id,
                            app_id=app.id,
                            binding_mode="development",
                            enabled=True,
                            installed_release_id=None,
                            created_by=owner,
                        )
                    )
                else:
                    binding.binding_mode = "development"
                    binding.enabled = True
                    binding.installed_release_id = None
                    binding.updated_at = datetime.utcnow()
                await session.flush()
                app_instance_path = get_app_instance_path(
                    lineage.project_id,
                    app.id,
                    workspace_root=self.workspace_root,
                )
                app_instance_existed = app_instance_path.exists()
                ensure_app_instance(
                    lineage.project_id,
                    app.id,
                    workspace_root=self.workspace_root,
                )

            lineage.status = "promoted"
            lineage.promoted_at = lineage.promoted_at or datetime.utcnow()
            lineage.discovery_json = {
                **(
                    lineage.discovery_json
                    if isinstance(lineage.discovery_json, Mapping)
                    else {}
                ),
                "promoted": True,
                "app_id": str(app.id),
                "app_entrypoint": app_entrypoint,
                "git_revision": checkpoint_revision,
                "last_action": action,
                "last_promoted_revision_id": (
                    str(lineage.current_revision_id)
                    if lineage.current_revision_id
                    else None
                ),
                "last_promoted_at": datetime.utcnow().isoformat(),
            }
            audit = ManagedToolPromotionAudit(
                id=uuid4(),
                lineage_id=lineage.id,
                app_id=app.id,
                revision_id=lineage.current_revision_id,
                action=action if action in {"promoted", "updated"} else "updated",
                actor_user_id=owner,
                evidence_json=dict(
                    evidence
                    or await self._promotion_evidence(session, lineage.id)
                ),
                policy_snapshot=policy.to_dict(),
                discovery_json=dict(lineage.discovery_json or {}),
            )
            session.add(audit)
            await session.flush()
            # Commit while the App workspace rollback journal and both
            # operation locks are still alive.
            await session.commit()
            journal.close()
            journal = None
            return app
        except BaseException:
            await session.rollback()
            if created_new_app and app_workspace is not None:
                if not _force_remove_tree(app_workspace):
                    logger.error(
                        "managed tool App rollback cleanup failed: app_id=%s",
                        getattr(app, "id", None),
                    )
            else:
                if (
                    git is not None
                    and previous_git_revision is not None
                    and checkpoint_revision is not None
                    and checkpoint_revision != previous_git_revision
                    and app is not None
                ):
                    try:
                        git.reset_to_revision(
                            app.id,
                            previous_git_revision,
                        )
                    except Exception:
                        logger.exception(
                            "managed tool App Git compensation failed: app_id=%s",
                            app.id,
                        )
                if journal is not None:
                    journal.rollback()
                    journal.close()
                    journal = None
            if (
                app_instance_path is not None
                and not app_instance_existed
                and app_instance_path.exists()
            ):
                _force_remove_tree(app_instance_path)
            raise
        finally:
            if journal is not None:
                journal.close()
            if app_lock is not None:
                app_lock.release()
            if project_lock is not None and project_lock_acquired_here:
                project_lock.release()

    @staticmethod
    def _manifest_command(runtime: str, entrypoint: str) -> str:
        return {
            "python": f"python {entrypoint}",
            "powershell": f"powershell -NoProfile -File {entrypoint}",
            "shell": f"sh {entrypoint}",
            "node": f"node {entrypoint}",
        }[runtime]

    # Friendly aliases used by tool/router integration and external callers.
    create_managed_tool = create_tool
    update_managed_tool = update_tool
    list_managed_tools = list_tools
    get_managed_tool = get_tool
    execute_managed_tool = execute_tool
    observe_execution = record_observation
    record_execution = record_observation
    promote_if_eligible = maybe_promote
    promote_lineage = maybe_promote


__all__ = [
    "ALLOWED_MANAGED_TOOL_RUNTIMES",
    "ManagedToolError",
    "ManagedToolNotFoundError",
    "ManagedToolAuthorizationError",
    "ManagedToolPathError",
    "ManagedToolPromotionPolicy",
    "ManagedToolExecutionResult",
    "ManagedToolExecutionAdapter",
    "SubprocessManagedToolExecutionAdapter",
    "ManagedToolService",
    "normalize_managed_tool_runtime",
]
