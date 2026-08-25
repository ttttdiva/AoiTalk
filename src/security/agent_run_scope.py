"""Run-scoped repository path security contract.

The tools used by a local coding agent are deliberately kept out of this
module.  They can depend on :class:`AgentRunScope` for one deterministic
answer to the question "may this path be used by this run?" and then perform
the actual I/O themselves.

The contract is intentionally fail-closed for mutations:

* the repository root is canonicalised once when a run starts;
* containment is checked with canonical paths (never string prefixes);
* existing symlink/junction/reparse components are resolved before a path is
  accepted; and
* a two-path operation (move, rename, copy) validates both sides.

This is a preflight policy.  Callers that need protection from a concurrent
rename should pair it with an OS primitive that holds a directory/file handle
while opening the target.  The policy still rejects an unresolved reparse
component rather than guessing.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence


PathLike = str | os.PathLike[str]
AccessKind = Literal["read", "write", "delete", "mutation"]


class RunScopeError(ValueError):
    """Base error for malformed or rejected run-scope paths."""


class RunScopeConfigurationError(RunScopeError):
    """Raised when a run is configured with an unsafe mutation scope."""


class RunScopeViolation(PermissionError, RunScopeError):
    """Raised when a requested path is outside this run's policy."""


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """Structured result returned by ``check_*`` helpers.

    ``path`` is canonical even when ``allowed`` is false.  It is useful for
    logging and for callers that want to avoid resolving the same path twice,
    while ``reason`` remains safe to show in a tool error.
    """

    allowed: bool
    path: Path
    scope: str
    reason: str = ""


def _path_key(path: Path) -> str:
    """Return a comparison key that handles Windows case-insensitivity.

    ``normcase`` is a no-op on POSIX and lowercases on Windows.  It also turns
    slash variants into the platform's normal separator before comparison.
    """

    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    """Return whether *path* is *root* or a descendant of *root*."""

    candidate_key = _path_key(path)
    root_key = _path_key(root)
    try:
        # commonpath is component-aware, unlike ``str.startswith``.  It also
        # raises for different Windows drives, which is correctly a denial.
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except (OSError, ValueError):
        return False


def _canonicalize(path: PathLike, *, base_dir: Path | None = None) -> Path:
    """Canonicalise a user path while permitting a missing final component."""

    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base_dir or Path.cwd()), raw)
    # realpath(strict=False) resolves every existing symlink/junction in the
    # path and leaves a missing tail intact.  abspath first also normalises
    # drive-relative Windows forms such as ``C:foo`` safely.
    return Path(os.path.realpath(os.path.abspath(raw)))


def _lexical_absolute(path: PathLike, *, base_dir: Path) -> Path:
    """Make an absolute, normalised path without resolving reparse points."""

    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base_dir), raw)
    return Path(os.path.normpath(os.path.abspath(raw)))


def _inspect_reparse_or_symlink(path: Path) -> tuple[bool, bool]:
    """Return ``(is_reparse, inspectable)`` for one existing component."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False, True
    except (NotADirectoryError, PermissionError, OSError):
        # An uninspectable component is not safe to treat as an ordinary
        # directory: a junction/reparse point may be hiding behind the error.
        return False, False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag), True


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return whether an existing path component is a link/reparse point."""

    is_reparse, inspectable = _inspect_reparse_or_symlink(path)
    return is_reparse or not inspectable


def _existing_components(path: Path) -> Iterable[Path]:
    """Yield existing components from the anchor through *path*.

    A path can have a missing tail (for a create operation), so checking only
    ``path`` is insufficient.  We stop at the first missing component; an
    existing ancestor is still checked.
    """

    anchor = Path(path.anchor) if path.anchor else Path.cwd().anchor
    current = Path(anchor) if anchor else Path()
    try:
        parts = path.relative_to(anchor).parts if anchor else path.parts
    except ValueError:
        parts = path.parts
        current = Path()
    for part in parts:
        current = current / part
        try:
            os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            # The path is not safely inspectable.  Yield it so the caller can
            # fail closed instead of treating it as a normal directory.
            yield current
            break
        yield current


def _has_unsafe_reparse_component(
    path: Path,
    root: Path,
    *,
    require_component_containment: bool = False,
) -> bool:
    """Detect a reparse component whose resolved target escapes *root*.

    A symlink/junction that resolves to another location *inside* the selected
    repository is safe for this path policy.  A component that cannot be
    inspected or resolved is unsafe and is denied conservatively.
    """

    for component in _existing_components(path):
        is_reparse, inspectable = _inspect_reparse_or_symlink(component)
        if not inspectable:
            return True
        if not is_reparse:
            continue
        if require_component_containment and not _is_within(component, root):
            return True
        try:
            resolved_component = _canonicalize(component)
        except (OSError, ValueError):
            return True
        if not _is_within(resolved_component, root):
            return True
    return False


def _default_identity(root: Path) -> str:
    digest = hashlib.sha256(_path_key(root).encode("utf-8", "surrogatepass")).hexdigest()
    return f"repo:{digest[:32]}"


def _normalise_roots(
    roots: Sequence[PathLike] | PathLike | None,
    *,
    default: Sequence[Path],
    base_dir: Path,
) -> tuple[Path, ...]:
    if roots is None:
        values: Sequence[PathLike] = default
    elif isinstance(roots, (str, os.PathLike)):
        values = (roots,)
    else:
        values = roots
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        resolved = _canonicalize(value, base_dir=base_dir)
        key = _path_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AgentRunScope:
    """Immutable security context for one local coding-agent run.

    ``target_root`` is canonicalised in ``__post_init__`` and is the only
    default mutation root.  Additional mutation roots must be explicitly
    declared as ``scratch_roots``; this prevents a caller from accidentally
    turning an arbitrary absolute path into a write scope.
    """

    target_root: PathLike
    repository_identity: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    baseline_revision: str | None = None
    baseline_git_state: Mapping[str, Any] = field(default_factory=dict)
    workspace_access_level: Literal["none", "read", "write"] = "write"
    read_roots: Sequence[PathLike] | PathLike | None = None
    write_roots: Sequence[PathLike] | PathLike | None = None
    delete_roots: Sequence[PathLike] | PathLike | None = None
    command_roots: Sequence[PathLike] | PathLike | None = None
    scratch_roots: Sequence[PathLike] | PathLike = field(default_factory=tuple)

    def __post_init__(self) -> None:
        root = _canonicalize(self.target_root)
        if not root.exists() or not root.is_dir():
            raise RunScopeConfigurationError(
                f"target repository root must be an existing directory: {self.target_root!s}"
            )
        if _has_unsafe_reparse_component(root, root):
            raise RunScopeConfigurationError(
                f"target repository root contains an escaping reparse point: {root}"
            )

        scratch = _normalise_roots(
            self.scratch_roots,
            default=(),
            base_dir=root,
        )
        # A scratch path is an explicit exception to the repository-root
        # boundary.  It must itself be canonical and cannot be a link to an
        # external location.
        for scratch_root in scratch:
            if _has_unsafe_reparse_component(scratch_root, scratch_root):
                raise RunScopeConfigurationError(
                    f"scratch root contains an escaping reparse point: {scratch_root}"
                )

        read_defaults = () if self.workspace_access_level == "none" else (root, *scratch)
        if self.read_roots is None:
            read_roots = _normalise_roots(None, default=read_defaults, base_dir=root)
        else:
            configured_read_roots: Sequence[PathLike]
            if isinstance(self.read_roots, (str, os.PathLike)):
                configured_read_roots = (root, *scratch, self.read_roots)
            else:
                configured_read_roots = (root, *scratch, *self.read_roots)
            read_roots = _normalise_roots(configured_read_roots, default=(), base_dir=root)
        mutation_defaults = (root, *scratch) if self.workspace_access_level == "write" else ()
        write_roots = _normalise_roots(self.write_roots, default=mutation_defaults, base_dir=root)
        delete_roots = _normalise_roots(self.delete_roots, default=mutation_defaults, base_dir=root)
        command_defaults = () if self.workspace_access_level == "none" else (root, *scratch)
        command_roots = _normalise_roots(self.command_roots, default=command_defaults, base_dir=root)

        mutation_allowed_roots = (root, *scratch)
        for name, configured in (
            ("write_roots", write_roots),
            ("delete_roots", delete_roots),
            ("command_roots", command_roots),
        ):
            if any(
                not any(_is_within(candidate, allowed) for allowed in mutation_allowed_roots)
                for candidate in configured
            ):
                raise RunScopeConfigurationError(
                    f"{name} may only contain target_root or explicit scratch_roots"
                )

        if self.workspace_access_level not in {"none", "read", "write"}:
            raise RunScopeConfigurationError(
                f"unsupported workspace_access_level: {self.workspace_access_level!r}"
            )

        object.__setattr__(self, "target_root", root)
        object.__setattr__(self, "repository_identity", self.repository_identity or _default_identity(root))
        object.__setattr__(self, "baseline_git_state", MappingProxyType(dict(self.baseline_git_state or {})))
        object.__setattr__(self, "read_roots", read_roots)
        object.__setattr__(self, "write_roots", write_roots)
        object.__setattr__(self, "delete_roots", delete_roots)
        object.__setattr__(self, "command_roots", command_roots)
        object.__setattr__(self, "scratch_roots", scratch)

    @property
    def canonical_root(self) -> Path:
        """Canonical selected repository root."""

        return self.target_root

    @property
    def repository_root(self) -> Path:
        """Alias used by repository/tool integrations."""

        return self.target_root

    @property
    def canonical_repository_root(self) -> Path:
        """Explicitly named alias for serialisers and run records."""

        return self.target_root

    @property
    def repo_identity(self) -> str:
        """Short alias for ``repository_identity``."""

        return str(self.repository_identity)

    @property
    def repository_id(self) -> str:
        """Alias used by persistence models that call the identity an ID."""

        return self.repo_identity

    @property
    def read_scope(self) -> tuple[Path, ...]:
        return self.read_roots

    @property
    def write_scope(self) -> tuple[Path, ...]:
        return self.write_roots

    @property
    def delete_scope(self) -> tuple[Path, ...]:
        return self.delete_roots

    @property
    def command_scope(self) -> tuple[Path, ...]:
        return self.command_roots

    @property
    def scratch_scope(self) -> tuple[Path, ...]:
        return self.scratch_roots

    @classmethod
    def for_repository(
        cls,
        target_root: PathLike,
        *,
        repository_identity: str | None = None,
        run_id: str | None = None,
        baseline_revision: str | None = None,
        baseline_git_state: Mapping[str, Any] | None = None,
        workspace_access_level: Literal["none", "read", "write"] = "write",
        read_roots: Sequence[PathLike] | PathLike | None = None,
        write_roots: Sequence[PathLike] | PathLike | None = None,
        delete_roots: Sequence[PathLike] | PathLike | None = None,
        command_roots: Sequence[PathLike] | PathLike | None = None,
        scratch_roots: Sequence[PathLike] | PathLike = (),
    ) -> "AgentRunScope":
        """Build the safe default scope for a selected repository."""

        kwargs: dict[str, Any] = {
            "target_root": target_root,
            "repository_identity": repository_identity,
            "baseline_revision": baseline_revision,
            "baseline_git_state": baseline_git_state or {},
            "workspace_access_level": workspace_access_level,
            "read_roots": read_roots,
            "write_roots": write_roots,
            "delete_roots": delete_roots,
            "command_roots": command_roots,
            "scratch_roots": scratch_roots,
        }
        if run_id is not None:
            kwargs["run_id"] = run_id
        return cls(**kwargs)

    def resolve(self, path: PathLike, *, base_dir: PathLike | None = None) -> Path:
        """Resolve a path against the selected repository by default."""

        base = _canonicalize(base_dir) if base_dir is not None else self.target_root
        return _canonicalize(path, base_dir=base)

    def _decision(self, path: PathLike, roots: Sequence[Path], scope: str) -> ScopeDecision:
        lexical_candidate = _lexical_absolute(path, base_dir=self.target_root)
        candidate = self.resolve(path)
        containment_root = next(
            (root for root in roots if _is_within(candidate, root)),
            self.target_root,
        )
        if _has_unsafe_reparse_component(
            lexical_candidate,
            containment_root,
            require_component_containment=True,
        ) or _has_unsafe_reparse_component(candidate, containment_root):
            return ScopeDecision(
                False,
                candidate,
                scope,
                "path traverses a symlink/junction/reparse point outside the target repository",
            )
        if any(_is_within(candidate, root) for root in roots):
            return ScopeDecision(True, candidate, scope)
        return ScopeDecision(
            False,
            candidate,
            scope,
            "canonical path is outside the configured scope",
        )

    @staticmethod
    def _raise(decision: ScopeDecision, operation: str) -> Path:
        if decision.allowed:
            return decision.path
        raise RunScopeViolation(
            f"run-scope denied {operation}: {decision.path} ({decision.reason})"
        )

    def check_read(self, path: PathLike) -> ScopeDecision:
        """Check read access.  Additional read roots are explicit and optional."""

        if self.workspace_access_level == "none":
            candidate = self.resolve(path)
            return ScopeDecision(False, candidate, "read", "workspace access level is none")
        return self._decision(path, self.read_roots, "read")

    def assert_read_allowed(self, path: PathLike) -> Path:
        return self._raise(self.check_read(path), "read")

    def is_read_allowed(self, path: PathLike) -> bool:
        return self.check_read(path).allowed

    def check_mutation(self, path: PathLike, operation: str = "write") -> ScopeDecision:
        """Check a write/create/overwrite/truncate/copy destination."""

        normalized_operation = operation.lower()
        if normalized_operation in {"delete", "remove", "rmdir"}:
            return self.check_delete(path)
        if self.workspace_access_level != "write":
            candidate = self.resolve(path)
            return ScopeDecision(
                False,
                candidate,
                "write",
                f"workspace access level is {self.workspace_access_level}",
            )
        decision = self._decision(path, self.write_roots, "write")
        if decision.allowed and _path_key(decision.path) == _path_key(self.target_root):
            if normalized_operation in {"rename", "move"}:
                return ScopeDecision(False, decision.path, "write", "repository root cannot be removed or moved")
        return decision

    def assert_mutation_allowed(self, path: PathLike, operation: str = "write") -> Path:
        return self._raise(self.check_mutation(path, operation), operation)

    def is_mutation_allowed(self, path: PathLike, operation: str = "write") -> bool:
        return self.check_mutation(path, operation).allowed

    def check_write(self, path: PathLike) -> ScopeDecision:
        """Alias for the generic mutation check."""

        return self.check_mutation(path, "write")

    def assert_write_allowed(self, path: PathLike) -> Path:
        """Alias for the generic mutation assertion."""

        return self.assert_mutation_allowed(path, "write")

    def check_delete(self, path: PathLike) -> ScopeDecision:
        """Check delete/truncate source access using the delete scope."""

        if self.workspace_access_level != "write":
            candidate = self.resolve(path)
            return ScopeDecision(
                False,
                candidate,
                "delete",
                f"workspace access level is {self.workspace_access_level}",
            )
        decision = self._decision(path, self.delete_roots, "delete")
        if decision.allowed and _path_key(decision.path) == _path_key(self.target_root):
            return ScopeDecision(False, decision.path, "delete", "repository root cannot be removed")
        return decision

    def assert_delete_allowed(self, path: PathLike) -> Path:
        return self._raise(self.check_delete(path), "delete")

    def is_delete_allowed(self, path: PathLike) -> bool:
        return self.check_delete(path).allowed

    def assert_move_allowed(self, source: PathLike, destination: PathLike) -> tuple[Path, Path]:
        """Validate both source deletion and destination write for a move."""

        source_path = self.assert_delete_allowed(source)
        destination_path = self.assert_mutation_allowed(destination, "move")
        return source_path, destination_path

    def assert_rename_allowed(self, source: PathLike, destination: PathLike) -> tuple[Path, Path]:
        """Rename is subject to the same two-sided policy as move."""

        return self.assert_move_allowed(source, destination)

    def assert_copy_allowed(self, source: PathLike, destination: PathLike) -> tuple[Path, Path]:
        """Validate source read access and destination write access for a copy."""

        source_path = self.assert_read_allowed(source)
        destination_path = self.assert_mutation_allowed(destination, "copy")
        return source_path, destination_path

    def check_command_cwd(self, cwd: PathLike | None = None) -> ScopeDecision:
        """Check a command working directory against the command scope."""

        candidate = self.target_root if cwd is None else cwd
        if self.workspace_access_level == "none":
            resolved = self.resolve(candidate)
            return ScopeDecision(False, resolved, "command", "workspace access level is none")
        decision = self._decision(candidate, self.command_roots, "command")
        if decision.allowed and decision.path.exists() and not decision.path.is_dir():
            return ScopeDecision(False, decision.path, "command", "command cwd is not a directory")
        return decision

    def assert_command_cwd_allowed(self, cwd: PathLike | None = None) -> Path:
        return self._raise(self.check_command_cwd(cwd), "command cwd")

    def is_command_cwd_allowed(self, cwd: PathLike | None = None) -> bool:
        return self.check_command_cwd(cwd).allowed

    def check_child_path(self, path: PathLike, access: AccessKind = "read") -> ScopeDecision:
        """Common child-process/path policy helper used by integrations."""

        if access == "read":
            return self.check_read(path)
        if access == "delete":
            return self.check_delete(path)
        if access in {"write", "mutation"}:
            return self.check_mutation(path, "child process")
        raise RunScopeConfigurationError(f"unsupported child path access: {access!r}")

    def assert_child_path_allowed(self, path: PathLike, access: AccessKind = "read") -> Path:
        decision = self.check_child_path(path, access)
        return self._raise(decision, f"child {access}")

    def check_path(self, path: PathLike, access: AccessKind = "read") -> ScopeDecision:
        """Alias used by adapters that expose one generic path validator."""

        return self.check_child_path(path, access)

    def assert_path_allowed(self, path: PathLike, access: AccessKind = "read") -> Path:
        """Alias used by adapters that expose one generic path validator."""

        return self.assert_child_path_allowed(path, access)

    def assert_command_paths_allowed(
        self,
        paths: Iterable[PathLike],
        *,
        access: AccessKind = "mutation",
    ) -> tuple[Path, ...]:
        """Validate known paths passed to a command or child process.

        Command *text* is intentionally not parsed here; regex inspection of
        shell text is not a security boundary.  The command executor should
        use this helper for every path it knows and enforce the OS boundary
        for arbitrary child-process mutations.
        """

        return tuple(self.assert_child_path_allowed(path, access) for path in paths)

    def child_path_policy(self, access: AccessKind = "read") -> Callable[[PathLike], Path]:
        """Return a small callback suitable for a child-process adapter."""

        if access not in {"read", "write", "delete", "mutation"}:
            raise RunScopeConfigurationError(f"unsupported child path access: {access!r}")
        return lambda path: self.assert_child_path_allowed(path, access)

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable run contract for events/audit logs."""

        return {
            "run_id": self.run_id,
            "target_root": str(self.target_root),
            "repository_identity": self.repository_identity,
            "baseline_revision": self.baseline_revision,
            "baseline_git_state": dict(self.baseline_git_state),
            "workspace_access_level": self.workspace_access_level,
            "read_roots": [str(path) for path in self.read_roots],
            "write_roots": [str(path) for path in self.write_roots],
            "delete_roots": [str(path) for path in self.delete_roots],
            "command_roots": [str(path) for path in self.command_roots],
            "scratch_roots": [str(path) for path in self.scratch_roots],
        }


# A ContextVar keeps the active scope attached to the request/task that owns a
# child run.  Integrations should call ``require_current_run_scope`` rather
# than silently falling back to the process cwd: no active scope means no
# coding-agent mutation authority.
_current_run_scope: ContextVar[AgentRunScope | None] = ContextVar(
    "aoi_agent_run_scope",
    default=None,
)


def get_current_run_scope() -> AgentRunScope | None:
    """Return the scope bound to the current execution context, if any."""

    return _current_run_scope.get()


def require_current_run_scope() -> AgentRunScope:
    """Return the active scope or fail closed when no run is bound."""

    scope = _current_run_scope.get()
    if scope is None:
        raise RunScopeViolation("no AgentRunScope is bound to the current execution context")
    return scope


def bind_run_scope(scope: AgentRunScope | None) -> Token[AgentRunScope | None]:
    """Bind *scope* for the current context and return a reset token."""

    if scope is not None and not isinstance(scope, AgentRunScope):
        raise TypeError("scope must be an AgentRunScope or None")
    return _current_run_scope.set(scope)


def reset_run_scope(token: Token[AgentRunScope | None]) -> None:
    """Restore the previous scope returned by :func:`bind_run_scope`."""

    _current_run_scope.reset(token)


@contextmanager
def run_scope_context(scope: AgentRunScope):
    """Temporarily bind a run scope for an agent/task execution."""

    token = bind_run_scope(scope)
    try:
        yield scope
    finally:
        reset_run_scope(token)


# Names used by integrations may evolve, but they should all refer to this
# same contract rather than each tool copying path checks.
RepositoryRunScope = AgentRunScope
RunScope = AgentRunScope


__all__ = [
    "AccessKind",
    "AgentRunScope",
    "bind_run_scope",
    "get_current_run_scope",
    "require_current_run_scope",
    "reset_run_scope",
    "RepositoryRunScope",
    "RunScope",
    "RunScopeConfigurationError",
    "RunScopeError",
    "RunScopeViolation",
    "run_scope_context",
    "ScopeDecision",
]
