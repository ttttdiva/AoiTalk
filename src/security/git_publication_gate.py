"""Fail-closed Git publication boundary for parent-controlled agent runs.

Workers may read and mutate the selected checkout, but publication remains a
parent-controller operation.  This module deliberately does not execute
``git commit``, ``git push``, ``git reset``, or ``git clean``.  It captures the
working-tree baseline at run start and performs a pure preflight against the
current state before a parent invokes its own publication transport.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .agent_run_scope import AgentRunScope, RunScopeViolation


class GitPublicationError(RuntimeError):
    """Base error for a malformed or unavailable publication preflight."""


class GitRepositoryError(GitPublicationError):
    """The selected path is not a usable Git repository."""


class WorkerPublicationDenied(GitPublicationError):
    """Raised when a worker attempts to publish or mutate Git history."""


class PublicationPreflightDenied(GitPublicationError):
    """Raised by :meth:`GitPublicationGate.assert_publishable` on denial."""


@dataclass(frozen=True, slots=True)
class GitStatusEntry:
    """One ``git status --porcelain=v1`` entry."""

    code: str
    path: str
    original_path: str | None = None

    @property
    def is_untracked(self) -> bool:
        return self.code == "??"

    @property
    def is_modified(self) -> bool:
        return not self.is_untracked

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.path, self.original_path) if self.original_path else (self.path,)

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "path": self.path}
        if self.original_path is not None:
            payload["original_path"] = self.original_path
        return payload


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Immutable Git baseline captured at the start of one agent run."""

    canonical_root: Path
    repository_identity: str
    baseline_head: str | None
    baseline_status: tuple[GitStatusEntry, ...] = ()
    baseline_modified: tuple[str, ...] = ()
    baseline_untracked: tuple[str, ...] = ()
    baseline_staged: tuple[str, ...] = ()
    baseline_worktree_hashes: Mapping[str, str | None] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    baseline_branch: str | None = None

    def __post_init__(self) -> None:
        root = _canonical_path(self.canonical_root)
        if not root.exists() or not root.is_dir():
            raise GitRepositoryError(f"repository root must be an existing directory: {root}")
        object.__setattr__(self, "canonical_root", root)
        object.__setattr__(self, "repository_identity", str(self.repository_identity or root))
        object.__setattr__(self, "baseline_status", tuple(self.baseline_status))
        object.__setattr__(self, "baseline_modified", tuple(_normalise_rel(p) for p in self.baseline_modified))
        object.__setattr__(self, "baseline_untracked", tuple(_normalise_rel(p) for p in self.baseline_untracked))
        object.__setattr__(self, "baseline_staged", tuple(_normalise_rel(p) for p in self.baseline_staged))
        object.__setattr__(
            self,
            "baseline_worktree_hashes",
            MappingProxyType({_normalise_rel(k): v for k, v in self.baseline_worktree_hashes.items()}),
        )

    @property
    def root(self) -> Path:
        return self.canonical_root

    @property
    def repository_root(self) -> Path:
        return self.canonical_root

    @property
    def repo_identity(self) -> str:
        return self.repository_identity

    @property
    def baseline_revision(self) -> str | None:
        return self.baseline_head

    @property
    def head(self) -> str | None:
        """Short alias for the baseline revision."""

        return self.baseline_head

    @property
    def status(self) -> tuple[GitStatusEntry, ...]:
        return self.baseline_status

    @property
    def modified(self) -> tuple[str, ...]:
        return self.baseline_modified

    @property
    def untracked(self) -> tuple[str, ...]:
        return self.baseline_untracked

    @property
    def baseline_git_state(self) -> dict[str, Any]:
        return {
            "head": self.baseline_head,
            "branch": self.baseline_branch,
            "status": [entry.as_dict() for entry in self.baseline_status],
            "modified": list(self.baseline_modified),
            "untracked": list(self.baseline_untracked),
            "staged": list(self.baseline_staged),
        }

    @property
    def baseline_paths(self) -> frozenset[str]:
        return frozenset(path for entry in self.baseline_status for path in entry.paths)

    @classmethod
    def capture(
        cls,
        repository_root: str | os.PathLike[str] | AgentRunScope,
        *,
        repository_identity: str | None = None,
        run_id: str | None = None,
    ) -> "RunSnapshot":
        """Capture canonical root, repository identity, HEAD and dirty state."""

        scope = repository_root if isinstance(repository_root, AgentRunScope) else None
        root = _canonical_repo_root(scope.canonical_root if scope is not None else repository_root)
        head = _git_output(root, ["rev-parse", "--verify", "HEAD"], allow_failure=True) or None
        branch = _git_output(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True) or None
        entries = _status_entries(root)
        modified = tuple(dict.fromkeys(path for e in entries if e.is_modified for path in e.paths))
        untracked = tuple(dict.fromkeys(path for e in entries if e.is_untracked for path in e.paths))
        staged = tuple(dict.fromkeys(path for e in entries if e.code[:1] not in {" ", "?"} for path in e.paths))
        hashes = {path: _worktree_hash(root, path) for e in entries for path in e.paths}
        return cls(
            canonical_root=root,
            repository_identity=repository_identity or (scope.repo_identity if scope is not None else _repository_identity(root)),
            baseline_head=head,
            baseline_status=entries,
            baseline_modified=modified,
            baseline_untracked=untracked,
            baseline_staged=staged,
            baseline_worktree_hashes=hashes,
            run_id=run_id or (scope.run_id if scope is not None else uuid.uuid4().hex),
            baseline_branch=branch,
        )

    @classmethod
    def from_repository(cls, repository_root: str | os.PathLike[str], **kwargs: Any) -> "RunSnapshot":
        return cls.capture(repository_root, **kwargs)

    @classmethod
    def from_scope(cls, scope: AgentRunScope, **kwargs: Any) -> "RunSnapshot":
        if not isinstance(scope, AgentRunScope):
            raise TypeError("scope must be an AgentRunScope")
        return cls.capture(scope, **kwargs)

    def refresh(self) -> "CurrentGitState":
        root = _canonical_repo_root(self.canonical_root)
        return CurrentGitState(
            canonical_root=root,
            head=_git_output(root, ["rev-parse", "--verify", "HEAD"], allow_failure=True) or None,
            branch=_git_output(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True) or None,
            status=_status_entries(root),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "canonical_root": str(self.canonical_root),
            "repository_identity": self.repository_identity,
            "baseline_head": self.baseline_head,
            "baseline_revision": self.baseline_revision,
            "baseline_branch": self.baseline_branch,
            "baseline_status": [entry.as_dict() for entry in self.baseline_status],
            "baseline_modified": list(self.baseline_modified),
            "baseline_untracked": list(self.baseline_untracked),
            "baseline_staged": list(self.baseline_staged),
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class CurrentGitState:
    """Read-only current state paired with a :class:`RunSnapshot`."""

    canonical_root: Path
    head: str | None
    branch: str | None
    status: tuple[GitStatusEntry, ...]

    @property
    def modified(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for e in self.status if e.is_modified for path in e.paths))

    @property
    def untracked(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for e in self.status if e.is_untracked for path in e.paths))

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for e in self.status for path in e.paths))

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_root": str(self.canonical_root),
            "head": self.head,
            "branch": self.branch,
            "status": [entry.as_dict() for entry in self.status],
            "modified": list(self.modified),
            "untracked": list(self.untracked),
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    """Structured, serialisable result of a parent publication preflight."""

    allowed: bool
    decision: str
    reason: str
    reasons: tuple[str, ...]
    run_id: str
    canonical_root: Path
    repository_identity: str
    baseline_head: str | None
    current_head: str | None
    baseline_modified: tuple[str, ...]
    baseline_untracked: tuple[str, ...]
    current_modified: tuple[str, ...]
    current_untracked: tuple[str, ...]
    introduced_paths: tuple[str, ...]
    preserved_preexisting_paths: tuple[str, ...]
    publishable_paths: tuple[str, ...]
    rejected_paths: tuple[str, ...]
    scope_confined: bool
    review_required: bool
    review_approved: bool
    worker_publication_denied: bool
    prohibited_actions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def denied(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "run_id": self.run_id,
            "canonical_root": str(self.canonical_root),
            "repository_identity": self.repository_identity,
            "baseline_head": self.baseline_head,
            "current_head": self.current_head,
            "baseline_modified": list(self.baseline_modified),
            "baseline_untracked": list(self.baseline_untracked),
            "current_modified": list(self.current_modified),
            "current_untracked": list(self.current_untracked),
            "introduced_paths": list(self.introduced_paths),
            "preserved_preexisting_paths": list(self.preserved_preexisting_paths),
            "publishable_paths": list(self.publishable_paths),
            "rejected_paths": list(self.rejected_paths),
            "scope_confined": self.scope_confined,
            "review_required": self.review_required,
            "review_approved": self.review_approved,
            "worker_publication_denied": self.worker_publication_denied,
            "prohibited_actions": list(self.prohibited_actions),
            "metadata": dict(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class WorkerPublicationDecision:
    """Unconditionally denied worker-side Git publication decision."""

    allowed: bool = False
    decision: str = "deny"
    reason: str = "worker publication is parent-controller only"
    worker_can_publish: bool = False
    commit_allowed: bool = False
    push_allowed: bool = False
    reset_allowed: bool = False
    clean_allowed: bool = False
    force_push_allowed: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": False,
            "decision": self.decision,
            "reason": self.reason,
            "worker_can_publish": False,
            "commit_allowed": False,
            "push_allowed": False,
            "reset_allowed": False,
            "clean_allowed": False,
            "force_push_allowed": False,
            "parent_gate_required": True,
            "metadata": dict(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    to_dict = as_dict


_PROHIBITED_ACTION_MARKERS = (
    "commit",
    "push",
    "force_push",
    "force-push",
    "reset",
    "clean",
    "branch_delete",
    "branch-delete",
    "rebase",
)


def worker_publication_decision(action: str = "publish") -> WorkerPublicationDecision:
    """Return a denial for every worker publication/history mutation request."""

    return WorkerPublicationDecision(
        metadata={"actor": "worker", "action": str(action or "publish")[:120], "parent_gate_required": True}
    )


def assert_worker_publication_denied(action: str = "publish") -> None:
    """Fail closed if a worker attempts publication or destructive Git state."""

    raise WorkerPublicationDenied(
        f"worker Git action denied: {str(action or 'publish').strip() or 'publish'}; parent controller gate required"
    )


class GitPublicationGate:
    """Parent-only, non-mutating Git publication preflight."""

    def __init__(
        self,
        snapshot: RunSnapshot,
        *,
        run_scope: AgentRunScope | None = None,
        allowed_paths: Iterable[str | os.PathLike[str]] | None = None,
        parent_actor: str = "parent_controller",
    ) -> None:
        if not isinstance(snapshot, RunSnapshot):
            raise TypeError("snapshot must be a RunSnapshot")
        if run_scope is not None and _canonical_path(run_scope.canonical_root) != snapshot.canonical_root:
            raise GitPublicationError("run_scope root must match the snapshot canonical repository root")
        self.snapshot = snapshot
        self.run_scope = run_scope
        self.parent_actor = str(parent_actor or "parent_controller").strip()
        self.allowed_paths = self._normalise_allowed_paths(allowed_paths)

    @property
    def worker_publication(self) -> WorkerPublicationDecision:
        return worker_publication_decision()

    def evaluate(
        self,
        *,
        review_approved: bool = False,
        review: Mapping[str, Any] | bool | None = None,
        review_ref: str | None = None,
        worker_actions: Iterable[str] | None = None,
        worker_command: str | None = None,
        worker_commands: Iterable[str] | None = None,
        worker_commit: bool = False,
        worker_push: bool = False,
        force_push: bool = False,
        reset_hard: bool = False,
        clean: bool = False,
        worker_report: Mapping[str, Any] | None = None,
        changed_scope: Iterable[str | os.PathLike[str]] | None = None,
        actor: str | None = None,
        current_state: CurrentGitState | None = None,
    ) -> PublicationDecision:
        """Return a structured allow/deny decision without running mutations."""

        reasons: list[str] = []
        actor_name = str(actor or self.parent_actor).strip()
        review_ok = _review_is_explicitly_approved(review, review_approved)
        if actor_name.casefold() != self.parent_actor.casefold():
            reasons.append("publication actor is not the configured parent controller")
        if not review_ok:
            reasons.append("explicit parent/Director review approval is required")

        supplied_actions = list(worker_actions or ())
        if worker_command:
            supplied_actions.append(worker_command)
        supplied_actions.extend(worker_commands or ())
        if worker_commit:
            supplied_actions.append("commit")
        if worker_push:
            supplied_actions.append("push")
        if force_push:
            supplied_actions.append("force-push")
        if reset_hard:
            supplied_actions.append("reset --hard")
        if clean:
            supplied_actions.append("clean")
        actions = _normalise_actions(supplied_actions)
        report_actions = _report_actions(worker_report)
        prohibited = tuple(dict.fromkeys(action for action in (*actions, *report_actions) if _is_prohibited_action(action)))
        if prohibited:
            reasons.append("worker attempted a prohibited Git history/publication action")
        if _report_grants_worker_publication(worker_report):
            reasons.append("worker publication metadata cannot grant commit or push authority")

        current = current_state or self.snapshot.refresh()
        if _canonical_path(current.canonical_root) != self.snapshot.canonical_root:
            reasons.append("current Git root differs from the run snapshot repository")
        if current.head != self.snapshot.baseline_head:
            reasons.append("current HEAD differs from the run baseline; worker commits or history rewrites are denied")

        baseline_paths = set(self.snapshot.baseline_paths)
        current_paths = set(current.paths)
        introduced = tuple(sorted(current_paths - baseline_paths, key=_path_sort_key))
        preserved = tuple(sorted(current_paths & baseline_paths, key=_path_sort_key))
        disappeared = tuple(sorted(baseline_paths - current_paths, key=_path_sort_key))

        # Any drift in a baseline path could include a user's pre-run change.
        # Refuse publication rather than silently staging it with worker code.
        protected_drift: list[str] = list(disappeared)
        for path in sorted(baseline_paths & current_paths, key=_path_sort_key):
            expected_hash = self.snapshot.baseline_worktree_hashes.get(path)
            if expected_hash is not None and _worktree_hash(self.snapshot.canonical_root, path) != expected_hash:
                protected_drift.append(path)
        protected_drift = list(dict.fromkeys(protected_drift))
        if protected_drift:
            reasons.append("pre-existing user changes were altered or removed")

        requested_paths = changed_scope if changed_scope is not None else self.allowed_paths
        allowed_keys, scope_errors = self._normalise_scope_paths(requested_paths)
        if scope_errors:
            reasons.extend(scope_errors)

        rejected: list[str] = []
        for path in introduced:
            if not self._path_confined(path):
                rejected.append(path)
                continue
            if allowed_keys is not None and _path_key(path) not in allowed_keys:
                rejected.append(path)
        if rejected:
            reasons.append("current diff contains paths outside the run publication scope")
        if not introduced and not protected_drift:
            reasons.append("no run-introduced changes are available for publication")

        publishable = tuple(
            sorted((path for path in introduced if path not in rejected and path not in protected_drift), key=_path_sort_key)
        )
        scope_confined = not rejected and not scope_errors and not protected_drift
        allowed = not reasons and bool(publishable)
        reason = "parent review and run-scoped diff approved" if allowed else (reasons[0] if reasons else "publication preflight denied")
        metadata = {
            "actor": actor_name,
            "review_ref": str(review_ref or "").strip() or None,
            "parent_gate_required": True,
            "worker_can_publish": False,
            "baseline_paths": sorted(baseline_paths, key=_path_sort_key),
            "disappeared_baseline_paths": disappeared,
            "protected_drift": tuple(protected_drift),
        }
        return PublicationDecision(
            allowed=allowed,
            decision="allow" if allowed else "deny",
            reason=reason,
            reasons=tuple(dict.fromkeys(reasons)),
            run_id=self.snapshot.run_id,
            canonical_root=self.snapshot.canonical_root,
            repository_identity=self.snapshot.repository_identity,
            baseline_head=self.snapshot.baseline_head,
            current_head=current.head,
            baseline_modified=self.snapshot.baseline_modified,
            baseline_untracked=self.snapshot.baseline_untracked,
            current_modified=current.modified,
            current_untracked=current.untracked,
            introduced_paths=introduced,
            preserved_preexisting_paths=tuple(sorted(set(preserved) | set(protected_drift), key=_path_sort_key)),
            publishable_paths=publishable,
            rejected_paths=tuple(sorted(set(rejected) | set(protected_drift), key=_path_sort_key)),
            scope_confined=scope_confined,
            review_required=True,
            review_approved=review_ok,
            worker_publication_denied=True,
            prohibited_actions=prohibited,
            metadata=metadata,
        )

    def assert_publishable(self, **kwargs: Any) -> PublicationDecision:
        decision = self.evaluate(**kwargs)
        if not decision.allowed:
            raise PublicationPreflightDenied(decision.reason)
        return decision

    # Read-only aliases used by coordinator integrations that call policy
    # checks ``check`` or ``preflight``.
    check = evaluate
    preflight = evaluate

    def _normalise_allowed_paths(self, paths: Iterable[str | os.PathLike[str]] | None) -> frozenset[str] | None:
        values, errors = self._normalise_scope_paths(paths)
        if errors:
            raise GitPublicationError("; ".join(errors))
        return None if values is None else frozenset(values)

    def _normalise_scope_paths(
        self,
        paths: Iterable[str | os.PathLike[str]] | None,
    ) -> tuple[set[str] | None, list[str]]:
        if paths is None:
            return None, []
        keys: set[str] = set()
        errors: list[str] = []
        for value in paths:
            candidate = Path(os.fspath(value))
            if not candidate.is_absolute():
                candidate = self.snapshot.canonical_root / candidate
            try:
                relative = _relative_repo_path(self.snapshot.canonical_root, candidate)
            except GitPublicationError as exc:
                errors.append(str(exc))
                continue
            if not self._path_confined(relative):
                errors.append(f"publication scope path escapes repository: {relative}")
                continue
            keys.add(_path_key(relative))
        return keys, errors

    def _path_confined(self, relative_path: str) -> bool:
        candidate = self.snapshot.canonical_root / Path(relative_path)
        try:
            if self.run_scope is not None:
                self.run_scope.assert_read_allowed(candidate)
                self.run_scope.assert_mutation_allowed(candidate, "publication")
                return True
            _relative_repo_path(self.snapshot.canonical_root, candidate)
            return _is_within(_canonical_path(candidate), self.snapshot.canonical_root)
        except (GitPublicationError, RunScopeViolation, OSError, ValueError):
            return False


def evaluate_parent_publication(snapshot: RunSnapshot, **kwargs: Any) -> PublicationDecision:
    return GitPublicationGate(snapshot).evaluate(**kwargs)


def capture_run_snapshot(repository_root: str | os.PathLike[str], **kwargs: Any) -> RunSnapshot:
    return RunSnapshot.capture(repository_root, **kwargs)


create_run_snapshot = capture_run_snapshot


def _canonical_path(path: str | os.PathLike[str] | Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def _canonical_repo_root(repository_root: str | os.PathLike[str]) -> Path:
    candidate = _canonical_path(repository_root)
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.exists() or not candidate.is_dir():
        raise GitRepositoryError(f"repository path must be an existing directory: {repository_root!s}")
    top = _git_output(candidate, ["rev-parse", "--show-toplevel"], allow_failure=False)
    if not top:
        raise GitRepositoryError(f"not a Git repository: {candidate}")
    resolved = _canonical_path(top)
    if not resolved.exists() or not resolved.is_dir():
        raise GitRepositoryError(f"Git repository root is unavailable: {resolved}")
    return resolved


def _git_output(root: Path, args: Sequence[str], *, allow_failure: bool) -> str | None:
    git = shutil.which("git")
    if not git:
        raise GitRepositoryError("git executable is unavailable")
    try:
        result = subprocess.run(
            [git, *args], cwd=root, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if allow_failure:
            return None
        raise GitRepositoryError(f"Git command failed: {' '.join(args)}") from exc
    value = (result.stdout or "").strip()
    if result.returncode != 0:
        if allow_failure:
            return None
        detail = (result.stderr or result.stdout or "").strip()
        raise GitRepositoryError(detail or f"Git command failed: {' '.join(args)}")
    return value


def _status_entries(root: Path) -> tuple[GitStatusEntry, ...]:
    git = shutil.which("git")
    if not git:
        raise GitRepositoryError("git executable is unavailable")
    try:
        result = subprocess.run(
            [git, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root, text=False, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitRepositoryError("Git status command failed") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise GitRepositoryError(detail or "Git status command failed")
    tokens = (result.stdout or b"").decode("utf-8", "surrogateescape").split("\0")
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token or len(token) < 3 or token[2] != " ":
            continue
        code = token[:2]
        path = _normalise_rel(token[3:])
        original = None
        if code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}:
            if index < len(tokens) and tokens[index]:
                original = _normalise_rel(tokens[index])
                index += 1
        entries.append(GitStatusEntry(code=code, path=path, original_path=original))
    return tuple(entries)


def _repository_identity(root: Path) -> str:
    remote = _git_output(root, ["config", "--get", "remote.origin.url"], allow_failure=True)
    if remote:
        return remote.strip()
    digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8", "surrogatepass")).hexdigest()
    return f"repo:{digest[:32]}"


def _normalise_rel(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value or "."


def _relative_repo_path(root: Path, candidate: Path) -> str:
    lexical = Path(os.path.normpath(os.path.abspath(candidate)))
    canonical_root = _canonical_path(root)
    try:
        lexical.relative_to(canonical_root)
    except ValueError as exc:
        raise GitPublicationError(f"path escapes repository root: {candidate}") from exc
    resolved = _canonical_path(lexical)
    try:
        resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise GitPublicationError(f"path resolves outside repository root: {candidate}") from exc
    return _normalise_rel(lexical.relative_to(canonical_root))


def _worktree_hash(root: Path, relative_path: str) -> str | None:
    try:
        rel = _relative_repo_path(root, root / Path(relative_path))
    except GitPublicationError:
        return None
    path = root / Path(rel)
    try:
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif path.is_file():
            data = path.read_bytes()
        else:
            return None
    except (OSError, ValueError):
        return None
    return hashlib.sha256(data).hexdigest()


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("\\", "/")))


def _path_sort_key(path: str) -> tuple[str, str]:
    return (_path_key(path), path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalise_actions(actions: Iterable[str] | None) -> tuple[str, ...]:
    if actions is None:
        return ()
    return tuple(dict.fromkeys(str(a or "").strip().lower().replace(" ", "_") for a in actions if str(a or "").strip()))


def _is_prohibited_action(action: str) -> bool:
    value = str(action or "").strip().lower().replace(" ", "_")
    return any(marker in value for marker in _PROHIBITED_ACTION_MARKERS)


def _report_actions(report: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not report:
        return ()
    values: list[str] = []
    for key in ("actions", "git_actions", "worker_actions", "operations"):
        value = report.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return _normalise_actions(values)


def _report_grants_worker_publication(report: Mapping[str, Any] | None) -> bool:
    if not report or not isinstance(report.get("publication"), Mapping):
        return False
    publication = report["publication"]
    # ``publication_allowed`` may be set by a parent after review; it is not
    # evidence that the worker itself was granted commit/push authority.
    return any(bool(publication.get(key)) for key in ("worker_can_publish", "commit_allowed", "push_allowed"))


def _review_is_explicitly_approved(review: Mapping[str, Any] | bool | None, flag: bool) -> bool:
    if flag is True or review is True:
        return True
    if not isinstance(review, Mapping):
        return False
    if any(review.get(key) is True for key in ("approved", "ok", "allow", "accepted")):
        return True
    status = str(review.get("status") or review.get("decision") or "").strip().lower()
    return status in {"approved", "approve", "ok", "accepted", "pass", "passed", "ok_task18"}


RepositoryRunSnapshot = RunSnapshot
ParentGitPublicationGate = GitPublicationGate
ParentPublicationGate = GitPublicationGate
PublicationGate = GitPublicationGate
GitPublicationDecision = PublicationDecision


__all__ = [
    "CurrentGitState", "GitPublicationError", "GitPublicationDecision", "GitPublicationGate",
    "GitRepositoryError", "GitStatusEntry", "ParentGitPublicationGate", "ParentPublicationGate", "PublicationDecision",
    "PublicationGate",
    "PublicationPreflightDenied", "RepositoryRunSnapshot", "RunSnapshot", "WorkerPublicationDecision",
    "WorkerPublicationDenied", "assert_worker_publication_denied", "capture_run_snapshot", "create_run_snapshot",
    "evaluate_parent_publication", "worker_publication_decision",
]
