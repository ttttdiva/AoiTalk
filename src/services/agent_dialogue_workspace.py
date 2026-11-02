"""Session-scoped workspace and provider-state fencing for Agent Dialogue.

An Agent identity may be bound to more than one ordinary Chat session.  The
identity is therefore not a workspace or provider-state key: every session
gets its own working directory and runtime scratch directory, while writes to
an explicitly shared Project/workspace resource acquire a bounded lease.

This module is intentionally an adapter boundary.  It does not issue an
``AgentRunScope`` and it never turns a path supplied by model text into an
authority.  A caller that needs native filesystem/shell execution must pass an
already issued ``AgentRunScope`` and explicitly opt in; otherwise the existing
provider gate can use its attested tool-free lane or deny before launch.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable
from uuid import UUID

from .app_operation_lock import (
    AppOperationLock,
    AppOperationLockTimeout,
    app_operation_lock,
)


AGENT_DIALOGUE_WORKSPACE_NAMESPACE = "_agent_dialogues"
AGENT_DIALOGUE_RUNTIME_NAMESPACE = "agent-dialogue"
# A finite default prevents a provider turn from waiting forever on another
# worker while still allowing ordinary short-lived turns to queue briefly.
DEFAULT_AGENT_DIALOGUE_LOCK_TIMEOUT = 300.0
MAX_AGENT_DIALOGUE_LOCK_TIMEOUT_SECONDS = 300.0
_WRITE_ROOT_LOCK_PREFIX = "agent-dialogue-write-root-"
_WRITE_ROOT_REGISTRY_LOCK = "agent-dialogue-write-roots-registry"
_WRITE_ROOT_POLL_INTERVAL = 0.05
_UNKNOWN_WRITE_ROOT_PATH = "<unknown-write-root>"
_UNKNOWN_WRITE_ROOT_RESOURCE_KEY = "<unknown-resource-key>"
_RESOURCE_KEY_PATTERN = re.compile(
    r"^(?:project:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|workspace:[0-9a-f]{64})$",
    re.IGNORECASE,
)


class AgentDialogueWorkspaceError(RuntimeError):
    """Base error for session workspace allocation and leasing."""


class AgentDialogueWorkspaceDenied(AgentDialogueWorkspaceError):
    """The requested workspace or native capability is not authorized."""


class AgentDialogueWorkspaceBusy(AgentDialogueWorkspaceError):
    """A shared writable workspace is leased by another session."""


class AgentDialogueNativeScopeError(AgentDialogueWorkspaceDenied):
    """Native execution was requested without an issued finite run scope."""


# Compatibility names used by integrations that describe the same boundary as
# a resource lease or workspace scope.
WorkspaceLeaseBusyError = AgentDialogueWorkspaceBusy
WorkspaceScopeError = AgentDialogueWorkspaceDenied


def _clean(value: Any, *, label: str, limit: int = 255) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentDialogueWorkspaceDenied(f"{label} is required")
    if len(text) > limit or "\x00" in text:
        raise AgentDialogueWorkspaceDenied(f"{label} is invalid")
    return text


def _sha(value: Any, *, length: int = 64) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        return True
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_safe_root(path: Path, *, label: str, create: bool = False) -> Path:
    """Validate one configured storage root without materialising it by default.

    ``Path.resolve`` alone is insufficient for an ACL boundary: an existing
    junction/symlink can make a lexical child appear to be inside the root.
    Reject every existing component of the configured root and of generated
    children before creating them.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.absolute()
    anchor = Path(candidate.anchor)
    current = anchor
    relative_parts = candidate.parts[1:] if anchor != candidate else ()
    for component in relative_parts:
        current = current / component
        if _is_reparse_or_symlink(current):
            raise AgentDialogueWorkspaceDenied(
                f"{label} cannot contain a symlink/junction/reparse point: {current}"
            )
    if candidate.exists() and not candidate.is_dir():
        raise AgentDialogueWorkspaceDenied(f"{label} is not a directory: {candidate}")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_symlink(candidate):
        raise AgentDialogueWorkspaceDenied(
            f"{label} cannot be a symlink/junction/reparse point: {candidate}"
        )
    return candidate.resolve(strict=False)


def _resolve_child(root: Path, candidate: Path, *, label: str) -> Path:
    """Resolve one generated child without creating it."""

    root = Path(root).resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise AgentDialogueWorkspaceDenied(f"configured root is not a directory: {root}")
    lexical = Path(candidate)
    if not lexical.is_absolute():
        lexical = root / lexical
    try:
        relative = lexical.absolute().relative_to(root)
    except ValueError as exc:
        raise AgentDialogueWorkspaceDenied(
            f"{label} is outside the configured workspace root"
        ) from exc
    current = root
    for component in relative.parts:
        current = current / component
        if _is_reparse_or_symlink(current):
            raise AgentDialogueWorkspaceDenied(
                f"{label} contains a symlink/junction/reparse point: {current}"
            )
    if current.exists() and not current.is_dir():
        raise AgentDialogueWorkspaceDenied(f"{label} is not a directory: {current}")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AgentDialogueWorkspaceDenied(
            f"{label} resolves outside the configured workspace root"
        ) from exc
    if resolved != current.absolute() and _is_reparse_or_symlink(current):
        raise AgentDialogueWorkspaceDenied(f"{label} resolves through a reparse point")
    return resolved


def _ensure_child(root: Path, candidate: Path, *, label: str) -> Path:
    """Materialise one generated child at an explicit write boundary."""

    resolved = _resolve_child(root, candidate, label=label)
    resolved.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_symlink(resolved):
        raise AgentDialogueWorkspaceDenied(
            f"{label} became a symlink/junction/reparse point during creation"
        )
    # Re-resolve after mkdir so a concurrent replacement cannot turn the
    # returned path into an alias.
    return _resolve_child(root, resolved, label=label)


def _canonical_existing_child(root: Path, candidate: Any, *, label: str) -> Path:
    """Canonicalise an existing path without creating or following aliases."""

    root = Path(root).resolve(strict=False)
    raw = Path(os.fspath(candidate)).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    try:
        relative = raw.absolute().relative_to(root)
    except ValueError as exc:
        raise AgentDialogueWorkspaceDenied(
            f"{label} is outside the configured workspace root"
        ) from exc
    current = root
    for component in relative.parts:
        current = current / component
        if _is_reparse_or_symlink(current):
            raise AgentDialogueWorkspaceDenied(
                f"{label} contains a symlink/junction/reparse point"
            )
    if not current.is_dir():
        raise AgentDialogueWorkspaceDenied(f"{label} is not an existing directory")
    resolved = current.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AgentDialogueWorkspaceDenied(
            f"{label} resolves outside the configured workspace root"
        ) from exc
    return resolved


def _project_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentDialogueWorkspaceDenied(
            "project_id must be a canonical UUID for shared workspace admission"
        ) from exc


def _validate_resource_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not _RESOURCE_KEY_PATTERN.fullmatch(key):
        raise AgentDialogueWorkspaceDenied("resource_key is not a canonical project/workspace key")
    return key


def _normalize_agent_dialogue_timeout(value: Any) -> float:
    """Validate and cap a dialogue lease timeout at the finite maximum."""

    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a finite non-negative number") from None
    if timeout < 0 or not math.isfinite(timeout):
        raise ValueError("timeout must be a finite non-negative number")
    return min(timeout, MAX_AGENT_DIALOGUE_LOCK_TIMEOUT_SECONDS)


def _write_root_key(path: Path) -> str:
    """Return the platform-normalised identity used by write-root leases."""

    return os.path.normcase(os.path.normpath(str(path)))


def _normalise_write_roots(
    value: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None,
) -> tuple[Path, ...]:
    """Canonicalise and de-duplicate explicit write roots.

    The lease identity is a canonical absolute path.  ``normcase`` is
    intentionally applied to the identity as well as the stored ``Path`` so
    Windows spellings that differ only by case or separators share a lock.
    Existing components are resolved, while a not-yet-created leaf remains a
    valid write root for callers that create it during the turn.
    """

    if value is None:
        return ()
    if isinstance(value, (str, os.PathLike)):
        items: Iterable[Any] = (value,)
    else:
        try:
            items = tuple(value)
        except TypeError as exc:
            raise AgentDialogueWorkspaceDenied("write_roots must be an iterable of paths") from exc

    roots: list[Path] = []
    seen: set[str] = set()
    for item in items:
        try:
            raw_value = os.fspath(item)
        except TypeError as exc:
            raise AgentDialogueWorkspaceDenied("write_roots must contain path-like values") from exc
        if isinstance(raw_value, bytes):
            try:
                raw_value = os.fsdecode(raw_value)
            except (UnicodeDecodeError, ValueError) as exc:
                raise AgentDialogueWorkspaceDenied("write root path is invalid") from exc
        if not isinstance(raw_value, str) or not raw_value.strip() or "\x00" in raw_value:
            raise AgentDialogueWorkspaceDenied("write root path is invalid")
        raw = Path(raw_value).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        try:
            canonical = raw.absolute().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise AgentDialogueWorkspaceDenied("write root path could not be normalised") from exc
        key = _write_root_key(canonical)
        if key in seen:
            continue
        seen.add(key)
        roots.append(Path(key))
    return tuple(roots)


def _write_roots_overlap(left: str, right: str) -> bool:
    """Whether two normalised paths are equal or one contains the other."""

    left = _write_root_key(Path(left))
    right = _write_root_key(Path(right))
    if left == right:
        return True
    separator = os.sep
    # ``normpath`` uses the native separator.  Keep an alternate check for
    # Windows callers that supplied a slash spelling before normalisation.
    left_prefix = left if left.endswith(separator) else left + separator
    right_prefix = right if right.endswith(separator) else right + separator
    return left.startswith(right_prefix) or right.startswith(left_prefix)


def _write_root_contains(parent: str, child: str) -> bool:
    """Whether *parent* is a strict ancestor of *child*."""

    parent = _write_root_key(Path(parent))
    child = _write_root_key(Path(child))
    if parent == child:
        return False
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _is_agent_run_scope(value: Any) -> bool:
    try:
        from ..security.agent_run_scope import AgentRunScope

        return isinstance(value, AgentRunScope)
    except Exception:
        return False


class _WorkspaceLeaseState:
    """Mutable, private lifetime marker shared by a client and its lease."""

    __slots__ = ("_tokens",)

    def __init__(self) -> None:
        self._tokens: set[object] = set()

    @property
    def active(self) -> bool:
        return bool(self._tokens)

    def enter(self) -> object:
        token = object()
        self._tokens.add(token)
        return token

    def exit(self, token: object) -> None:
        self._tokens.discard(token)


_TASK_OWNERSHIP_ISSUER = object()


class _TaskOwnershipState:
    """Private lifetime marker shared by every copy of one task ownership."""

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


@dataclass(frozen=True, slots=True)
class CanonicalTaskOwnership:
    """Server-issued proof that a checkpointed API task owns its workspace use.

    This proves *task ownership*, not an exclusive cwd lock: concurrent tasks
    sharing a cwd rely on their disjoint edit scopes.  It is only constructed
    by :meth:`AgentDialogueWorkspaceManager.task_session`; request, model and
    metadata values can never create one.  It ends with the session (worker,
    background cleanup and drain included) and when cancellation is requested.
    """

    task_id: str
    work_item_id: str
    run_id: str
    attempt: int
    owner_id: str
    session_id: str
    agent_id: str
    resource_key: str
    _cancelled: Callable[[], bool] = field(repr=False, compare=False)
    _state: _TaskOwnershipState = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._issuer is not _TASK_OWNERSHIP_ISSUER:
            raise AgentDialogueWorkspaceDenied("task ownership can only be issued by the workspace manager")

    @property
    def live(self) -> bool:
        if not self._state.active:
            return False
        try:
            return not bool(self._cancelled())
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class TaskOwnershipSpec:
    """Server-side request to run a checkpointed canonical task without the cwd mutex."""

    task_id: str
    work_item_id: str
    attempt: int
    cancelled: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class AgentDialogueWorkspace:
    """Immutable per-session workspace/provider isolation snapshot."""

    session_id: str
    agent_id: str
    provider_id: str
    model_id: str
    workspace_path: Path
    scratch_path: Path
    run_id: str | None = None
    owner_id: str | None = None
    project_id: str | None = None
    resource_key: str | None = None
    readonly: bool = False
    native_allowed: bool = False
    native_scope: Any | None = field(default=None, repr=False, compare=False)
    _lease_state: _WorkspaceLeaseState = field(
        default_factory=_WorkspaceLeaseState,
        repr=False,
        compare=False,
    )
    _task_ownership: CanonicalTaskOwnership | None = field(default=None, repr=False, compare=False)
    write_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _clean(self.session_id, label="session_id"))
        object.__setattr__(self, "agent_id", _clean(self.agent_id, label="agent_id"))
        object.__setattr__(self, "provider_id", _clean(self.provider_id, label="provider_id"))
        object.__setattr__(self, "model_id", _clean(self.model_id, label="model_id"))
        if self.run_id is not None:
            object.__setattr__(self, "run_id", _clean(self.run_id, label="run_id"))
        if self.owner_id is not None:
            object.__setattr__(self, "owner_id", _clean(self.owner_id, label="owner_id"))
        workspace = Path(self.workspace_path).expanduser().absolute()
        scratch = Path(self.scratch_path).expanduser().absolute()
        if self.resource_key is not None:
            object.__setattr__(self, "resource_key", _validate_resource_key(self.resource_key))
        if self.project_id is not None:
            object.__setattr__(self, "project_id", _project_uuid(self.project_id))
        normalized_write_roots = _normalise_write_roots(self.write_roots)
        object.__setattr__(self, "write_roots", normalized_write_roots)
        object.__setattr__(self, "workspace_path", workspace)
        object.__setattr__(self, "scratch_path", scratch)
        object.__setattr__(self, "readonly", bool(self.readonly))
        object.__setattr__(self, "native_allowed", bool(self.native_allowed))
        if self.native_scope is not None and not _is_agent_run_scope(self.native_scope):
            raise AgentDialogueNativeScopeError(
                "native workspace authority must be an issued AgentRunScope"
            )
        if self.native_allowed and self.native_scope is None:
            raise AgentDialogueNativeScopeError(
                "native_allowed requires an issued AgentRunScope proof"
            )
        if self.native_scope is not None:
            scope_run_id = str(getattr(self.native_scope, "run_id", "") or "")
            if not self.run_id or scope_run_id != self.run_id:
                raise AgentDialogueNativeScopeError(
                    "native workspace and AgentRunScope run_id must match"
                )
            try:
                decision = self.native_scope.check_command_cwd(workspace)
            except Exception as exc:
                raise AgentDialogueNativeScopeError(
                    "native workspace authority could not be validated"
                ) from exc
            if not decision.allowed or Path(decision.path).resolve() != workspace.resolve():
                raise AgentDialogueNativeScopeError(
                    "workspace is outside the issued AgentRunScope"
                )
        if self.readonly and self.write_roots:
            raise AgentDialogueWorkspaceDenied(
                "readonly workspace cannot hold write roots"
            )
        if self.readonly and self.resource_key is not None:
            raise AgentDialogueWorkspaceDenied(
                "readonly workspace cannot hold a shared write resource lease"
            )

    @property
    def working_directory(self) -> Path:
        """Provider cwd for framework/tool executions."""

        return self.workspace_path

    @property
    def runtime_cache_directory(self) -> Path:
        """Session-specific runtime scratch directory."""

        return self.scratch_path

    @property
    def lease_active(self) -> bool:
        """Whether at least one owning workspace lease is still held."""

        return self._lease_state.active

    @property
    def task_ownership(self) -> CanonicalTaskOwnership | None:
        """Canonical task-owner proof, or None for an ordinary (mutex) workspace."""

        return self._task_ownership

    @property
    def context_alive(self) -> bool:
        return self.lease_active

    def assert_lease_active(self) -> None:
        if not self.lease_active:
            raise AgentDialogueWorkspaceDenied(
                "Agent Dialogue workspace lease is not active"
            )

    def assert_provider_entry(self) -> None:
        """Revalidate lease and any native scope at a provider boundary."""

        self.assert_lease_active()
        if self.native_allowed:
            assert_native_execution_allowed(self)

    @property
    def provider_state_key(self) -> str:
        """Stable non-secret key for provider continuation/state caches."""

        payload = {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "workspace": str(self.workspace_path),
        }
        return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    @property
    def session_key(self) -> str:
        return _sha(f"{self.agent_id}:{self.session_id}")

    def as_dict(self) -> dict[str, Any]:
        """Bounded audit projection; no raw provider handles or prompt data."""

        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "workspace_path": str(self.workspace_path),
            "scratch_path": str(self.scratch_path),
            "project_id": self.project_id,
            "resource_key": self.resource_key,
            "write_roots": [str(path) for path in self.write_roots],
            "readonly": self.readonly,
            "native_allowed": self.native_allowed,
            "lease_active": self.lease_active,
            "provider_state_key": self.provider_state_key,
        }


_current_workspace: contextvars.ContextVar[AgentDialogueWorkspace | None] = contextvars.ContextVar(
    "agent_dialogue_workspace",
    default=None,
)


def get_current_agent_dialogue_workspace() -> AgentDialogueWorkspace | None:
    """Return the workspace bound to the current execution task."""

    return _current_workspace.get()


def bind_agent_dialogue_workspace(
    workspace: AgentDialogueWorkspace | None,
) -> contextvars.Token[AgentDialogueWorkspace | None]:
    if workspace is not None and not isinstance(workspace, AgentDialogueWorkspace):
        raise TypeError("workspace must be an AgentDialogueWorkspace or None")
    return _current_workspace.set(workspace)


def reset_agent_dialogue_workspace(
    token: contextvars.Token[AgentDialogueWorkspace | None],
) -> None:
    _current_workspace.reset(token)


def require_agent_dialogue_workspace() -> AgentDialogueWorkspace:
    workspace = get_current_agent_dialogue_workspace()
    if workspace is None:
        raise AgentDialogueWorkspaceDenied("Agent Dialogue workspace is not bound")
    return workspace


def assert_native_execution_allowed(
    workspace: AgentDialogueWorkspace | None = None,
) -> AgentDialogueWorkspace:
    selected = workspace or get_current_agent_dialogue_workspace()
    if selected is None:
        raise AgentDialogueNativeScopeError(
            "native Agent execution requires an explicitly authorized AgentRunScope"
        )
    try:
        selected.assert_lease_active()
    except AgentDialogueWorkspaceDenied as exc:
        raise AgentDialogueNativeScopeError(
            "native Agent execution requires an active workspace lease"
        ) from exc
    if not selected.native_allowed or not _is_agent_run_scope(selected.native_scope):
        raise AgentDialogueNativeScopeError(
            "native Agent execution requires an explicitly authorized AgentRunScope"
        )
    scope_run_id = str(getattr(selected.native_scope, "run_id", "") or "")
    workspace_run_id = str(selected.run_id or "")
    if not workspace_run_id or scope_run_id != workspace_run_id:
        raise AgentDialogueNativeScopeError(
            "native Agent workspace and AgentRunScope run_id do not match"
        )
    try:
        from ..security.agent_run_scope import get_current_run_scope

        current_scope = get_current_run_scope()
    except Exception as exc:
        raise AgentDialogueNativeScopeError(
            "current AgentRunScope is unavailable"
        ) from exc
    if current_scope is not selected.native_scope:
        raise AgentDialogueNativeScopeError(
            "current AgentRunScope is not the issued workspace scope"
        )
    if str(getattr(current_scope, "run_id", "") or "") != workspace_run_id:
        raise AgentDialogueNativeScopeError(
            "current AgentRunScope run_id does not match the workspace run_id"
        )
    try:
        from .agent_run_service import get_current_agent_run_id

        current_run_id = str(get_current_agent_run_id() or "")
    except Exception as exc:
        raise AgentDialogueNativeScopeError(
            "current Agent execution identity is unavailable"
        ) from exc
    if current_run_id != workspace_run_id:
        raise AgentDialogueNativeScopeError(
            "current Agent execution run_id does not match the workspace run_id"
        )
    try:
        decision = selected.native_scope.check_command_cwd(selected.workspace_path)
    except Exception as exc:
        raise AgentDialogueNativeScopeError(
            "native workspace authority could not be revalidated"
        ) from exc
    if not decision.allowed:
        raise AgentDialogueNativeScopeError(
            "selected workspace is outside the issued AgentRunScope"
        )
    return selected


class AgentDialogueWorkspaceManager:
    """Allocate isolated session paths and bounded shared-resource leases."""

    def __init__(
        self,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        runtime_cache_root: str | os.PathLike[str] | None = None,
        lock_timeout: float = DEFAULT_AGENT_DIALOGUE_LOCK_TIMEOUT,
    ) -> None:
        configured_root = workspace_root or os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
        # Validate the lexical configured path before resolving it.  Resolving
        # first would turn a user supplied junction/symlink into its target and
        # silently weaken the storage-root ACL.
        self.workspace_root = _assert_safe_root(
            Path(configured_root),
            label="workspace_root",
        )
        cache_value = runtime_cache_root or (Path.cwd() / "cache")
        self.runtime_cache_root = _assert_safe_root(
            Path(cache_value), label="runtime_cache_root"
        )
        try:
            self.lock_timeout = _normalize_agent_dialogue_timeout(lock_timeout)
        except ValueError as exc:
            raise ValueError("lock_timeout must be a finite non-negative number") from exc

    def _workspace_namespace(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
    ) -> Path:
        agent_part = f"agent_{_sha(agent_id, length=32)}"
        session_part = f"session_{_sha(session_id, length=32)}"
        if project_id is not None:
            project = _project_uuid(project_id)
            return (
                self.workspace_root
                / "_projects"
                / f"project_{project}"
                / AGENT_DIALOGUE_WORKSPACE_NAMESPACE
                / agent_part
                / session_part
            )
        return self.workspace_root / AGENT_DIALOGUE_WORKSPACE_NAMESPACE / agent_part / session_part

    def _scratch_namespace(
        self,
        *,
        agent_id: str,
        session_id: str,
        provider_id: str,
        model_id: str,
        run_id: str | None,
    ) -> Path:
        return (
            self.runtime_cache_root
            / AGENT_DIALOGUE_RUNTIME_NAMESPACE
            / f"agent_{_sha(agent_id, length=32)}"
            / f"session_{_sha(session_id, length=32)}"
            / f"provider_{_sha(provider_id, length=24)}"
            / f"model_{_sha(model_id, length=24)}"
            / f"run_{_sha(run_id or 'session', length=24)}"
        )

    def _validate_workspace_override(self, workspace_path: Any) -> Path:
        """Reject arbitrary path overrides at the Agent boundary.

        The only path override accepted by :meth:`allocate` is the exact root
        of an already issued ``AgentRunScope``.  Ordinary Dialogue sessions
        always use the deterministic session namespace below ``workspace_root``.
        """

        raise AgentDialogueWorkspaceDenied(
            "workspace_path override requires an exact issued AgentRunScope"
        )

    @staticmethod
    def _exact_scope_root(scope: Any, *, require_write: bool = False) -> Path:
        if not _is_agent_run_scope(scope):
            raise AgentDialogueNativeScopeError(
                "workspace_path override requires an issued AgentRunScope"
            )
        root = Path(scope.canonical_root).resolve()
        try:
            scoped_root = (
                scope.assert_mutation_allowed(root, "write")
                if require_write
                else scope.assert_read_allowed(root)
            )
        except Exception as exc:
            raise AgentDialogueNativeScopeError(
                "AgentRunScope workspace boundary could not be checked"
            ) from exc
        if root != Path(scoped_root).resolve():
            raise AgentDialogueNativeScopeError(
                "workspace_path is not the exact issued AgentRunScope root"
            )
        if not root.is_dir() or _is_reparse_or_symlink(root):
            raise AgentDialogueWorkspaceDenied(
                "issued AgentRunScope root is not a safe directory"
            )
        return root

    def resource_key_for(
        self,
        *,
        project_id: str | UUID | None = None,
        workspace_path: str | os.PathLike[str] | Path | None = None,
        readonly: bool = False,
    ) -> str | None:
        """Return the canonical shared resource key for an authorized target."""

        if readonly:
            return None
        if workspace_path is None and project_id is not None:
            return f"project:{_project_uuid(project_id)}"
        if workspace_path is None:
            raise AgentDialogueWorkspaceDenied(
                "a writable workspace requires project_id or workspace_path"
            )
        # A raw path is intentionally not a resource authority.  Callers use
        # a canonical workspace hash issued by the transport/bridge or pass a
        # Project UUID above.
        self._validate_workspace_override(workspace_path)
        raise AgentDialogueWorkspaceDenied("unreachable workspace key override")

    def resource_key_for_canonical_workspace(
        self,
        workspace_path: str | os.PathLike[str] | Path,
        *,
        authorized_scope: Any | None = None,
    ) -> str:
        """Return the raw workspace key for a server-selected canonical root.

        Transport/Bridge callers may use an existing Project workspace path or
        an issued AgentRunScope root to fence an ordinary Human writer against
        an Agent turn.  This helper only computes a lock identity; it never
        creates a workspace or issues filesystem authority.
        """

        if authorized_scope is not None:
            exact = self._exact_scope_root(authorized_scope)
            supplied = Path(os.fspath(workspace_path)).expanduser().absolute().resolve()
            if supplied != exact:
                raise AgentDialogueWorkspaceDenied(
                    "workspace key path must equal the exact issued AgentRunScope root"
                )
            target = exact
        else:
            target = _canonical_existing_child(
                self.workspace_root,
                workspace_path,
                label="canonical workspace",
            )
        return f"workspace:{_sha(os.path.normcase(str(target)))}"

    def allocate(
        self,
        *,
        session_id: str,
        agent_id: str,
        provider_id: str,
        model_id: str,
        run_id: str | None = None,
        owner_id: str | None = None,
        project_id: str | UUID | None = None,
        readonly: bool = False,
        write_roots: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
        workspace_path: str | os.PathLike[str] | Path | None = None,
        resource_key: str | None = None,
        native_allowed: bool = False,
        authorized_scope: Any | None = None,
    ) -> AgentDialogueWorkspace:
        """Create one session context under the canonical storage roots.

        Ordinary calls use a deterministic session namespace.  A path is only
        accepted when it is the exact root of an already issued
        ``AgentRunScope``; request/model paths never become authority.
        """

        sid = _clean(session_id, label="session_id")
        aid = _clean(agent_id, label="agent_id")
        provider = _clean(provider_id, label="provider_id")
        model = _clean(model_id, label="model_id")
        run = _clean(run_id, label="run_id") if run_id is not None else None
        owner = _clean(owner_id, label="owner_id") if owner_id is not None else None
        project = _project_uuid(project_id) if project_id is not None else None
        is_readonly = bool(readonly)
        normalized_write_roots = _normalise_write_roots(write_roots)
        if is_readonly and normalized_write_roots:
            raise AgentDialogueWorkspaceDenied(
                "readonly workspace cannot hold write roots"
            )

        generated_workspace = self._workspace_namespace(
            agent_id=aid,
            session_id=sid,
            project_id=project,
        )
        if workspace_path is None and authorized_scope is None:
            selected_workspace = _ensure_child(
                self.workspace_root,
                generated_workspace,
                label="session workspace",
            )
        elif authorized_scope is not None:
            selected_workspace = self._exact_scope_root(
                authorized_scope,
                require_write=not is_readonly,
            )
            if workspace_path is not None:
                supplied = Path(os.fspath(workspace_path)).expanduser().absolute().resolve()
                if supplied != selected_workspace:
                    raise AgentDialogueNativeScopeError(
                        "workspace_path must equal the exact issued AgentRunScope root"
                    )
        else:
            # A model/request path is never promoted to a workspace authority.
            self._validate_workspace_override(workspace_path)
            raise AgentDialogueWorkspaceDenied("unreachable workspace override")
        selected_scratch = _ensure_child(
            self.runtime_cache_root,
            self._scratch_namespace(
                agent_id=aid,
                session_id=sid,
                provider_id=provider,
                model_id=model,
                run_id=run,
            ),
            label="session runtime cache",
        )

        if is_readonly:
            expected_key = None
        elif project is not None:
            expected_key = f"project:{project}"
        else:
            expected_key = f"workspace:{_sha(os.path.normcase(str(selected_workspace.resolve())))}"
        if resource_key is not None:
            supplied_key = _validate_resource_key(resource_key)
            if supplied_key != expected_key:
                raise AgentDialogueWorkspaceDenied(
                    "resource_key does not match the canonical project/workspace target"
                )
        elif expected_key is not None and not is_readonly:
            # The context carries the shared key for writes.  Readonly contexts
            # intentionally carry no key and never acquire the resource lock.
            resource_key = expected_key

        if native_allowed and authorized_scope is None:
            raise AgentDialogueNativeScopeError(
                "native workspace authority must come from AgentRunScope"
            )
        if authorized_scope is not None and not _is_agent_run_scope(authorized_scope):
            raise AgentDialogueNativeScopeError(
                "workspace authority must come from AgentRunScope"
            )
        native_scope = authorized_scope if native_allowed else None

        return AgentDialogueWorkspace(
            session_id=sid,
            agent_id=aid,
            provider_id=provider,
            model_id=model,
            workspace_path=selected_workspace,
            scratch_path=selected_scratch,
            run_id=run,
            owner_id=owner,
            project_id=project,
            resource_key=None if is_readonly else resource_key,
            write_roots=normalized_write_roots,
            readonly=is_readonly,
            native_allowed=bool(native_allowed),
            native_scope=native_scope,
        )

    # Common names for adapters that prefer an allocation verb.
    create = allocate
    create_for_session = allocate
    build = allocate
    workspace_for_session = allocate
    get_resource_key = resource_key_for

    def _lock_for(self, resource_key: str) -> AppOperationLock:
        key = _validate_resource_key(resource_key)
        # Use a Dialogue-specific raw-resource lock for both Project and
        # workspace keys.  Domain Project/Docs APIs retain their own
        # ``project_operation_lock`` inside the mediated mutation call; holding
        # that domain lock across the whole provider turn would be re-entrant
        # and can deadlock.
        return app_operation_lock(
            f"agent-dialogue-{_sha(key)}",
            workspace_root=self.workspace_root,
        )

    def _write_root_lock_for(self, root: Path) -> AppOperationLock:
        """Return the cross-process lock for one canonical write root."""

        return app_operation_lock(
            f"{_WRITE_ROOT_LOCK_PREFIX}{_sha(_write_root_key(root))}",
            workspace_root=self.workspace_root,
        )

    @staticmethod
    def _read_write_root_metadata(
        metadata_path: Path,
    ) -> tuple[str, str | None] | None:
        """Read the root and optional resource identity from one sidecar."""

        try:
            raw = metadata_path.read_bytes()
        except (FileNotFoundError, OSError):
            return None
        raw = raw.strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
            stored = payload.get("root")
        except (AttributeError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(stored, str):
            return None
        try:
            normalized = _normalise_write_roots((stored,))
        except AgentDialogueWorkspaceDenied:
            return None
        if not normalized:
            return None
        if "resource_key" not in payload:
            resource_key = _UNKNOWN_WRITE_ROOT_RESOURCE_KEY
        else:
            resource_key = payload.get("resource_key")
        if resource_key is not None and resource_key != _UNKNOWN_WRITE_ROOT_RESOURCE_KEY:
            try:
                resource_key = _validate_resource_key(resource_key)
            except AgentDialogueWorkspaceDenied:
                # Preserve the path marker for root fencing, but treat an
                # invalid/missing resource identity as unknown.  A legacy
                # resource lease then fails closed against this active marker.
                resource_key = _UNKNOWN_WRITE_ROOT_RESOURCE_KEY
        return _write_root_key(normalized[0]), resource_key

    def _ensure_write_root_metadata(
        self,
        lock: AppOperationLock,
        root: Path,
        resource_key: str | None,
    ) -> None:
        """Create metadata before locking a new root marker.

        The registry lock serialises this operation across processes.  The
        lock itself remains held for the whole provider turn, so a later
        process can inspect the path and test whether the marker is active.
        """

        lock_path = lock.path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_symlink(lock_path):
            raise AgentDialogueWorkspaceDenied(
                "write root lock file cannot be a symlink/junction"
            )
        metadata_path = lock_path.with_name(f"{lock_path.name}.json")
        if _is_reparse_or_symlink(metadata_path):
            raise AgentDialogueWorkspaceDenied(
                "write root lock metadata cannot be a symlink/junction"
            )
        root_key = _write_root_key(root)
        stored = self._read_write_root_metadata(metadata_path)
        if stored is not None:
            stored_root, stored_resource_key = stored
            if stored_root != root_key or stored_resource_key != resource_key:
                raise AgentDialogueWorkspaceDenied(
                    "write root lock identity collision"
                )
            return
        if metadata_path.exists() and metadata_path.stat().st_size:
            raise AgentDialogueWorkspaceDenied(
                "write root lock metadata is invalid"
            )
        payload = json.dumps(
            {"root": root_key, "resource_key": resource_key},
            ensure_ascii=False,
        ).encode("utf-8")
        temporary = metadata_path.with_name(f"{metadata_path.name}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, metadata_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_root_markers(
        self,
    ) -> tuple[tuple[str, str | None, AppOperationLock, Path, Path], ...]:
        """List known write-root markers and their shared lock objects."""

        lock_dir = self.workspace_root / ".locks"
        try:
            paths = tuple(lock_dir.glob(f"app_{_WRITE_ROOT_LOCK_PREFIX}*.lock.json"))
        except (FileNotFoundError, OSError):
            return ()
        markers: list[tuple[str, str | None, AppOperationLock, Path, Path]] = []
        prefix = "app_"
        suffix = ".lock.json"
        for metadata_path in paths:
            if _is_reparse_or_symlink(metadata_path):
                raise AgentDialogueWorkspaceDenied(
                    "write root lock metadata cannot be a symlink/junction"
                )
            name = metadata_path.name
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            lock_name = name[: -len(".json")]
            lock_path = metadata_path.with_name(lock_name)
            if _is_reparse_or_symlink(lock_path):
                raise AgentDialogueWorkspaceDenied(
                    "write root lock file cannot be a symlink/junction"
                )
            lock_id = lock_name[len(prefix) : -len(".lock")]
            stored = self._read_write_root_metadata(metadata_path)
            if stored is None:
                marker_root = _UNKNOWN_WRITE_ROOT_PATH
                marker_resource_key = _UNKNOWN_WRITE_ROOT_RESOURCE_KEY
            else:
                marker_root, marker_resource_key = stored
            markers.append(
                (
                    marker_root,
                    marker_resource_key,
                    app_operation_lock(lock_id, workspace_root=self.workspace_root),
                    lock_path,
                    metadata_path,
                )
            )
        return tuple(markers)

    @staticmethod
    def _prune_write_root_marker(
        lock: AppOperationLock,
        lock_path: Path,
        metadata_path: Path,
    ) -> None:
        """Remove an inactive marker while the registry gate is held."""

        # ``lock`` has just been probed successfully and released.  Every
        # manager acquisition goes through the registry, so no new owner can
        # observe this marker between the probe and these unlinks.
        for path in (metadata_path, lock_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # A stale marker is only an optimisation.  Leave it for a
                # later registry pass if the filesystem cannot remove it now.
                pass

    @staticmethod
    async def _write_root_lock_is_active(lock: AppOperationLock) -> bool:
        """Probe a marker without retaining the probe handle."""

        try:
            await lock.acquire(timeout=0.0)
        except AppOperationLockTimeout:
            return True
        lock.release()
        return False

    def _write_root_registry_lock(self) -> AppOperationLock:
        return app_operation_lock(
            _WRITE_ROOT_REGISTRY_LOCK,
            workspace_root=self.workspace_root,
        )

    async def _write_root_conflicts(
        self,
        roots: tuple[Path, ...],
    ) -> bool:
        candidate_keys = tuple(_write_root_key(root) for root in roots)
        for (
            marker_key,
            _marker_resource_key,
            marker_lock,
            lock_path,
            metadata_path,
        ) in self._write_root_markers():
            if await self._write_root_lock_is_active(marker_lock):
                if (
                    marker_key == _UNKNOWN_WRITE_ROOT_PATH
                    or any(
                        _write_roots_overlap(candidate, marker_key)
                        for candidate in candidate_keys
                    )
                ):
                    return True
                continue
            self._prune_write_root_marker(marker_lock, lock_path, metadata_path)
        return False

    async def _resource_marker_conflicts(self, resource_key: str) -> bool:
        """Return whether an active path lease fences this resource key."""

        key = _validate_resource_key(resource_key)
        for (
            _marker_key,
            marker_resource_key,
            marker_lock,
            lock_path,
            metadata_path,
        ) in self._write_root_markers():
            if await self._write_root_lock_is_active(marker_lock):
                # A marker without a valid resource identity may have been
                # written by an older version.  Treat it as conflicting with
                # every legacy resource lease until it is released.
                if (
                    marker_resource_key == _UNKNOWN_WRITE_ROOT_RESOURCE_KEY
                    or marker_resource_key == key
                ):
                    return True
                continue
            self._prune_write_root_marker(marker_lock, lock_path, metadata_path)
        return False

    async def _acquire_write_root_locks(
        self,
        roots: tuple[Path, ...],
        *,
        resource_key: str | None,
        timeout: float | None,
    ) -> tuple[AppOperationLock, ...]:
        """Atomically reserve all overlapping write roots for one lease."""

        if not roots:
            return ()
        limit = self.lock_timeout if timeout is None else _normalize_agent_dialogue_timeout(timeout)
        deadline = time.monotonic() + limit

        # If one lease names a parent and one of its children, the parent lock
        # alone fences both.  This also avoids self-conflict while acquiring a
        # multi-root lease and gives every acquisition one deterministic order.
        lock_roots = tuple(
            root
            for root in roots
            if not any(
                _write_root_contains(_write_root_key(other), _write_root_key(root))
                for other in roots
                if other != root
            )
        )
        lock_roots = tuple(
            sorted(lock_roots, key=lambda root: _write_root_key(root))
        )
        registry = self._write_root_registry_lock()

        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                # A zero caller timeout still gets one short registry turn;
                # otherwise unrelated sibling roots can lose a race on the
                # coordination lock before their path overlap is inspected.
                await registry.acquire(
                    timeout=_WRITE_ROOT_POLL_INTERVAL if remaining <= 0 else remaining
                )
            except AppOperationLockTimeout as exc:
                raise AgentDialogueWorkspaceBusy(
                    "shared Agent write roots are busy"
                ) from exc

            acquired: list[AppOperationLock] = []
            conflict = False
            success = False
            try:
                conflict = await self._write_root_conflicts(lock_roots)
                if not conflict and resource_key is not None:
                    conflict = await self._write_root_lock_is_active(
                        self._lock_for(resource_key)
                    )
                if not conflict:
                    for root in lock_roots:
                        lock = self._write_root_lock_for(root)
                        self._ensure_write_root_metadata(lock, root, resource_key)
                        try:
                            await lock.acquire(timeout=0.0)
                        except AppOperationLockTimeout:
                            conflict = True
                            break
                        acquired.append(lock)
                    if not conflict:
                        success = True
                        return tuple(acquired)
            finally:
                registry.release()
                if not success:
                    for lock in reversed(acquired):
                        lock.release()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentDialogueWorkspaceBusy(
                    "shared Agent write roots are busy"
                )
            await asyncio.sleep(min(_WRITE_ROOT_POLL_INTERVAL, remaining))

    @asynccontextmanager
    async def _write_root_lease(
        self,
        roots: tuple[Path, ...],
        *,
        resource_key: str | None,
        timeout: float | None,
    ) -> AsyncIterator[None]:
        locks = await self._acquire_write_root_locks(
            roots,
            resource_key=resource_key,
            timeout=timeout,
        )
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    @asynccontextmanager
    async def resource_lease(
        self,
        resource_key: str,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """Acquire a canonical resource lease without an Agent workspace.

        Ordinary Human Chat admission uses this same seam when it is writing a
        Project/workspace resource.  Keeping the lock identity independent of
        AgentRun/ConversationSession lets the transport fence an already
        active Human turn against an Agent turn; the bridge still performs the
        durable WorkItem/Run late recheck before provider execution.
        """

        key = _validate_resource_key(resource_key)
        lock = self._lock_for(key)
        limit = self.lock_timeout if timeout is None else _normalize_agent_dialogue_timeout(timeout)
        deadline = time.monotonic() + limit
        registry = self._write_root_registry_lock()

        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                await registry.acquire(
                    timeout=_WRITE_ROOT_POLL_INTERVAL if remaining <= 0 else remaining
                )
            except AppOperationLockTimeout as exc:
                raise AgentDialogueWorkspaceBusy(
                    f"shared Agent workspace is busy: {key}"
                ) from exc

            acquired = False
            conflict = False
            try:
                conflict = await self._resource_marker_conflicts(key)
                if not conflict:
                    try:
                        await lock.acquire(timeout=0.0)
                    except AppOperationLockTimeout:
                        conflict = True
                    else:
                        acquired = True
            finally:
                registry.release()

            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentDialogueWorkspaceBusy(
                    f"shared Agent workspace is busy: {key}"
                )
            await asyncio.sleep(min(_WRITE_ROOT_POLL_INTERVAL, remaining))

        try:
            yield key
        finally:
            lock.release()

    acquire_resource = resource_lease

    @asynccontextmanager
    async def lease(
        self,
        workspace: AgentDialogueWorkspace,
        *,
        timeout: float | None = None,
        owner_id: str | None = None,
        write_roots: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
    ) -> AsyncIterator[AgentDialogueWorkspace]:
        """Hold the optional shared write lease for one turn.

        Readonly/session-private turns do not acquire a shared lock.  An
        explicit ``write_roots`` lease fences only overlapping paths; callers
        without roots retain the legacy resource-key lease.  When supplied to
        this method, ``write_roots`` must equal the allocation's canonical
        roots.  Lock acquisition has a finite default and cancellation is
        propagated after the underlying AppOperationLock has cleaned its
        worker handle.
        """

        if not isinstance(workspace, AgentDialogueWorkspace):
            raise TypeError("workspace must be an AgentDialogueWorkspace")
        if owner_id is not None and workspace.owner_id not in (None, str(owner_id)):
            raise AgentDialogueWorkspaceDenied("workspace lease owner mismatch")
        if write_roots is None:
            selected_write_roots = workspace.write_roots
        else:
            selected_write_roots = _normalise_write_roots(write_roots)
            if selected_write_roots != workspace.write_roots:
                raise AgentDialogueWorkspaceDenied(
                    "lease write_roots must match the workspace write_roots"
                )
        if workspace.readonly and selected_write_roots:
            raise AgentDialogueWorkspaceDenied(
                "readonly workspace cannot hold write roots"
            )

        @asynccontextmanager
        async def bound_workspace() -> AsyncIterator[AgentDialogueWorkspace]:
            lease_token = workspace._lease_state.enter()
            token = bind_agent_dialogue_workspace(workspace)
            try:
                yield workspace
            finally:
                reset_agent_dialogue_workspace(token)
                workspace._lease_state.exit(lease_token)

        @asynccontextmanager
        async def write_roots_and_workspace() -> AsyncIterator[AgentDialogueWorkspace]:
            async with self._write_root_lease(
                selected_write_roots,
                resource_key=workspace.resource_key,
                timeout=timeout,
            ):
                async with bound_workspace():
                    yield workspace

        if selected_write_roots:
            # Explicit roots replace the legacy project/workspace-wide mutex;
            # the resource_key remains an identity/audit field.  This is what
            # permits two disjoint files in one repository to run together.
            async with write_roots_and_workspace():
                yield workspace
            return

        if workspace.resource_key and not workspace.readonly:
            async with self.resource_lease(workspace.resource_key, timeout=timeout):
                async with bound_workspace():
                    yield workspace
            return

        async with bound_workspace():
            yield workspace

    @asynccontextmanager
    async def session(
        self,
        *,
        session_id: str,
        agent_id: str,
        provider_id: str,
        model_id: str,
        run_id: str | None = None,
        owner_id: str | None = None,
        project_id: str | UUID | None = None,
        readonly: bool = False,
        write_roots: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
        workspace_path: str | os.PathLike[str] | Path | None = None,
        resource_key: str | None = None,
        native_allowed: bool = False,
        authorized_scope: Any | None = None,
        timeout: float | None = None,
        task_ownership: TaskOwnershipSpec | None = None,
    ) -> AsyncIterator[AgentDialogueWorkspace]:
        workspace = self.allocate(
            session_id=session_id,
            agent_id=agent_id,
            provider_id=provider_id,
            model_id=model_id,
            run_id=run_id,
            owner_id=owner_id,
            project_id=project_id,
            readonly=readonly,
            write_roots=write_roots,
            workspace_path=workspace_path,
            resource_key=resource_key,
            native_allowed=native_allowed,
            authorized_scope=authorized_scope,
        )
        if task_ownership is not None and not workspace.readonly and not workspace.write_roots:
            # Every allocate() check above already ran.  Ownership replaces the
            # cwd mutex for this writable workspace only; it is not an
            # exclusive-lock claim.
            async with self._task_owned_lease(workspace, task_ownership):
                yield workspace
            return
        if task_ownership is not None and not workspace.readonly:
            # Task ownership remains the liveness/provenance proof, while
            # explicit write roots still require their own path lease.
            async with self._write_root_lease(
                workspace.write_roots,
                resource_key=workspace.resource_key,
                timeout=timeout,
            ):
                async with self._task_owned_lease(workspace, task_ownership):
                    yield workspace
            return
        async with self.lease(workspace, timeout=timeout, owner_id=owner_id):
            yield workspace

    @asynccontextmanager
    async def _task_owned_lease(
        self,
        workspace: AgentDialogueWorkspace,
        spec: TaskOwnershipSpec,
    ) -> AsyncIterator[AgentDialogueWorkspace]:
        if not isinstance(spec, TaskOwnershipSpec) or not callable(spec.cancelled):
            raise AgentDialogueWorkspaceDenied("task ownership spec is invalid")
        if not workspace.resource_key or not workspace.resource_key.startswith("workspace:"):
            raise AgentDialogueWorkspaceDenied("task ownership requires a canonical workspace key")
        if not workspace.run_id or not workspace.owner_id:
            raise AgentDialogueWorkspaceDenied("task ownership requires run and owner identity")
        state = _TaskOwnershipState()
        ownership = CanonicalTaskOwnership(
            task_id=_clean(spec.task_id, label="task_id"),
            work_item_id=_clean(spec.work_item_id, label="work_item_id"),
            run_id=workspace.run_id,
            attempt=int(spec.attempt),
            owner_id=workspace.owner_id,
            session_id=workspace.session_id,
            agent_id=workspace.agent_id,
            resource_key=workspace.resource_key,
            _cancelled=spec.cancelled,
            _state=state,
            _issuer=_TASK_OWNERSHIP_ISSUER,
        )
        object.__setattr__(workspace, "_task_ownership", ownership)
        lease_token = workspace._lease_state.enter()
        token = bind_agent_dialogue_workspace(workspace)
        try:
            yield workspace
        finally:
            state.active = False
            reset_agent_dialogue_workspace(token)
            workspace._lease_state.exit(lease_token)


# Short aliases for transport/adapter code and tests.
AgentDialogueWorkspaceContext = AgentDialogueWorkspace
AgentDialogueWorkspaceLease = AgentDialogueWorkspaceManager
AgentDialogueWorkspaceService = AgentDialogueWorkspaceManager
AgentWorkspaceManager = AgentDialogueWorkspaceManager
get_current_workspace = get_current_agent_dialogue_workspace
require_current_workspace = require_agent_dialogue_workspace


@asynccontextmanager
async def shared_resource_lease(
    resource_key: str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_AGENT_DIALOGUE_LOCK_TIMEOUT,
) -> AsyncIterator[str]:
    """Transport-friendly shared lease for Agent and ordinary Human turns."""

    normalized_timeout = _normalize_agent_dialogue_timeout(timeout)
    manager = AgentDialogueWorkspaceManager(
        workspace_root=workspace_root,
        lock_timeout=normalized_timeout,
    )
    async with manager.resource_lease(resource_key, timeout=normalized_timeout):
        yield _validate_resource_key(resource_key)


__all__ = [
    "AGENT_DIALOGUE_RUNTIME_NAMESPACE",
    "AGENT_DIALOGUE_WORKSPACE_NAMESPACE",
    "DEFAULT_AGENT_DIALOGUE_LOCK_TIMEOUT",
    "MAX_AGENT_DIALOGUE_LOCK_TIMEOUT_SECONDS",
    "AgentDialogueNativeScopeError",
    "AgentDialogueWorkspace",
    "AgentDialogueWorkspaceBusy",
    "AgentDialogueWorkspaceContext",
    "AgentDialogueWorkspaceDenied",
    "AgentDialogueWorkspaceError",
    "AgentDialogueWorkspaceLease",
    "AgentDialogueWorkspaceManager",
    "AgentDialogueWorkspaceService",
    "AgentWorkspaceManager",
    "CanonicalTaskOwnership",
    "TaskOwnershipSpec",
    "WorkspaceLeaseBusyError",
    "WorkspaceScopeError",
    "assert_native_execution_allowed",
    "bind_agent_dialogue_workspace",
    "get_current_agent_dialogue_workspace",
    "get_current_workspace",
    "require_agent_dialogue_workspace",
    "require_current_workspace",
    "reset_agent_dialogue_workspace",
    "shared_resource_lease",
]
